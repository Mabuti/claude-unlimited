"""Profile repository: the only place that coordinates config.json + Keychain.

Callers must never be able to create a half-saved Profile (metadata written
but no credential, or the reverse). This module owns that coordination and
rolls back on partial failure. Nothing here makes a network call, so it is
pure local state management and runs without the proxy.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Optional

# anthropic_oauth imports nothing from this package (stdlib only), so this
# has no circular-import risk. Needed here for resolve_profile_own_identity()
# — see its docstring for why this module, not the gateway, is what verifies
# a Profile's own organization before a login is allowed to touch it.
from . import activity, anthropic_oauth, connectors, oauth_credential, secret_store
from . import config as config_module
from .config import CONFIG_LOCK, Pool, Profile, load_pool, save_pool

# Derived from connectors.py, so a kind's name is registered in exactly one
# place. Both stay flat tuples shared across kinds; the per-kind auth_mode
# check lives in _validate() below.
_VALID_KINDS = tuple(connectors.CONNECTORS)
_VALID_AUTH_MODES = connectors.all_auth_modes()
# Must stay in sync with static/app.js's TAG_COLORS: that is the picker the
# Dashboard shows and this is what accepts or rejects what it sends. A color
# in only one of the two either can't be picked or is rejected on save.
_TAG_COLORS = ("#43C6FF", "#35D07F", "#FFB020", "#C97BFF", "#FF5FA6", "#2FD9C4", "#FF5C5C", "#FFD93D", "#6C8EFF", "#5C5C63")


class ValidationError(ValueError):
    pass


class ProfileRepositoryError(RuntimeError):
    pass


def _new_id() -> str:
    return secrets.token_hex(8)


def _validate(name: str, kind: str, base_url: Optional[str], auth_mode: str, tag_color: Optional[str]) -> None:
    if not name or not name.strip():
        raise ValidationError("Profile name is required.")
    if len(name) > 80:
        raise ValidationError("Profile name is too long (max 80 characters).")
    if kind not in _VALID_KINDS:
        raise ValidationError(f"Unknown Profile kind {kind!r}; must be one of {_VALID_KINDS}.")
    if kind in ("api", "codex") and auth_mode not in _VALID_AUTH_MODES:
        raise ValidationError(f"Unknown auth_mode {auth_mode!r}; must be one of {_VALID_AUTH_MODES}.")
    if base_url:
        if not re.match(r"^https://[^\s]+$", base_url):
            raise ValidationError(
                "base_url must start with https:// (loopback http:// is not accepted here; "
                "this validates the UPSTREAM target, a different trust boundary than the "
                "daemon's own local listener)."
            )
    if tag_color is not None and tag_color not in _TAG_COLORS:
        raise ValidationError(f"Unknown tag_color; must be one of {_TAG_COLORS}.")


# Directories a Profile is allowed to name. Both are created by cli.py under
# this tool's own app directory, and delete_profile() removes codex_home
# outright — so an unconstrained value here is a request to recursively
# delete a directory of someone's choosing. The roots come from config.py so
# the code that CREATES them and the code that VALIDATES them cannot drift.
def _validate_isolated_dir(value, field: str, root) -> None:
    from pathlib import Path
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty path, or null.")
    # resolve() both sides: a symlink or a ".." segment must not be able to
    # escape the root, and the root itself may be a symlink on some setups.
    try:
        candidate = Path(value).expanduser().resolve()
        resolved_root = root.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"{field} is not a usable path: {exc}") from None
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise ValidationError(f"{field} must be inside {root}.")


def _validate_field_types(**changes) -> None:
    """Type- and range-checks every field a caller may set.

    This is not defensive nicety. `config.py`'s load_pool() coerces on read —
    `int(p["priority"])`, `float(p["switch_threshold"])` — so a value that is
    merely SAVED without checking becomes an exception on the next load, and
    load_pool() is called by every API handler, the proxy path, and the very
    request that would fix it. One bad PATCH could leave the daemon unable to
    start until someone hand-edited config.json.
    """
    from .openai_models import VALID_REASONING_EFFORTS

    def _num(key, *, kind, minimum=None, maximum=None, allow_none=False):
        if key not in changes:
            return
        value = changes[key]
        if value is None:
            if allow_none:
                return
            raise ValidationError(f"{key} cannot be null.")
        # bool is an int subclass; accepting True as a priority of 1 would be
        # a silent, confusing success.
        if isinstance(value, bool) or not isinstance(value, kind):
            wanted = ("a number" if isinstance(kind, tuple)
                      else {"int": "a whole number", "str": "text"}.get(kind.__name__, kind.__name__))
            raise ValidationError(f"{key} must be {wanted}, got {type(value).__name__}.")
        if minimum is not None and value < minimum:
            raise ValidationError(f"{key} must be at least {minimum}.")
        if maximum is not None and value > maximum:
            raise ValidationError(f"{key} must be at most {maximum}.")

    _num("priority", kind=int, minimum=1)
    _num("switch_threshold", kind=(int, float), minimum=0, maximum=100)
    _num("token_threshold", kind=int, minimum=0, allow_none=True)
    _num("monthly_budget_cap", kind=(int, float), minimum=0, allow_none=True)

    for key in ("enabled", "automatic"):
        if key in changes and not isinstance(changes[key], bool):
            raise ValidationError(f"{key} must be true or false.")

    for key in ("name", "kind", "auth_mode"):
        if key in changes and not isinstance(changes[key], str):
            raise ValidationError(f"{key} must be a string.")

    for key in ("base_url", "default_model", "tag_color", "plan", "codex_model"):
        if key in changes and changes[key] is not None and not isinstance(changes[key], str):
            raise ValidationError(f"{key} must be a string, or null.")

    effort = changes.get("codex_reasoning_effort")
    if effort is not None and effort not in VALID_REASONING_EFFORTS:
        raise ValidationError(
            f"codex_reasoning_effort must be one of {list(VALID_REASONING_EFFORTS)}.")

    claude_root, codex_root = config_module.accounts_roots()
    _validate_isolated_dir(changes.get("claude_config_dir"), "claude_config_dir", claude_root)
    _validate_isolated_dir(changes.get("codex_home"), "codex_home", codex_root)


def list_profiles() -> list[Profile]:
    return load_pool().profiles


def find_by_account_uuid(account_uuid: str) -> Optional[Profile]:
    """Dedup key for OAuth Profiles: re-importing or re-running
    `add-account` for an already-added account must update its credential
    rather than create a duplicate row."""
    return next((p for p in load_pool().profiles if p.account_uuid == account_uuid), None)


def find_by_account_and_org(account_uuid: str, org_uuid: Optional[str],
                             *, profiles: Optional[list[Profile]] = None) -> tuple[Optional[Profile], bool]:
    """Pair-aware identity lookup for OAuth Profiles.

    `account_uuid` alone is not a unique key: one Anthropic account_uuid can
    surface under more than one organization.uuid — a personal Max plan and
    an organization (Team) seat on the same email address return the SAME
    account.uuid but a DIFFERENT organization.uuid (measured 2026-09-22).
    Deduping on account_uuid alone therefore collapses two separate,
    separately-billed subscriptions into one Profile, silently overwriting
    the first with the second. Identity here is the pair.

    Returns (profile, needs_org_backfill) with exactly three outcomes:
      - Exact match: a Profile with this account_uuid AND this org_uuid
        already exists -> (profile, False). Reuse it as-is.
      - Legacy match: no exact match, but a Profile with this account_uuid
        and org_uuid=None exists -> (profile, True). Reuse it, but signal
        that the caller must backfill org_uuid/organization_type onto it.
        This is what stops a Profile added before this pair became the
        identity from being duplicated the next time its account re-auths —
        without it, every pre-existing Profile would look like a "no match"
        and spawn a duplicate on its very next credential refresh.
      - No match: (None, False). The caller should create a new Profile.

    An exact match always wins over a legacy match when both exist for this
    account_uuid — the pool is scanned for an exact pair match in full before
    a legacy candidate is considered, so which one is stored first in the
    pool never decides the outcome.

    If the `org_uuid` ARGUMENT is None (the caller has no organization
    information for this login — e.g. a bundle or a caller that never
    resolved one), this falls back to matching on account_uuid alone, the
    same single-key behaviour as find_by_account_uuid(), rather than
    refusing to match: a caller that cannot supply an org must never be made
    to create a duplicate just because it doesn't know the org.

    `profiles` lets a caller that already holds an in-progress, unsaved Pool
    snapshot (export_import.apply_import(), which mutates a local `pool`
    across a whole bundle before saving it once) match against that snapshot
    instead of re-reading config.json from disk. Omitted, this reads the
    live pool the same way find_by_account_uuid() does.
    """
    candidates = list(load_pool().profiles if profiles is None else profiles)
    matching_uuid = [p for p in candidates if p.account_uuid == account_uuid]
    if not matching_uuid:
        return None, False
    if org_uuid is None:
        return matching_uuid[0], False
    for p in matching_uuid:
        if p.org_uuid == org_uuid:
            return p, False
    for p in matching_uuid:
        if p.org_uuid is None:
            return p, True
    return None, False


def resolve_profile_own_identity(profile: Profile) -> Optional[anthropic_oauth.AccountProfile]:
    """Resolves a Profile's own identity from ITS OWN stored credential —
    never from whatever a NEXT login happens to report.

    2026-09-22 incident this exists to prevent: a legacy (org_uuid=None)
    Profile was matched on account_uuid alone, and the NEXT login's
    organization was trusted to decide what that pre-existing Profile was.
    One Anthropic account_uuid can hold more than one differently-organized
    subscription (a Team seat and a personal Max plan), so that trust was
    misplaced — it let a Team credential get silently overwritten by an
    unrelated personal-plan login that merely shared the account_uuid. The
    fix is to ask the Profile's OWN credential what it is, instead.

    Contract callers may rely on:
      - NEVER raises. Every failure — no stored token, a decode error,
        ProfileLookupError, a network error, anything at all — returns None.
        Fold every exception in, don't special-case any of them: a caller
        deciding whether to touch someone's credential must never be taken
        down by an unrelated exception type it didn't anticipate.
      - NEVER refreshes the token. Uses the stored CURRENT access token
        only, exactly as oauth_credential.decode() returns it. The daemon's
        Gateway owns refresh (gateway.py); a second refresher here would
        race it on refresh-token rotation and could invalidate the Profile
        entirely — a read-only identity check must never risk that.
      - NEVER logs or prints the token — it is held in a local variable and
        passed straight to fetch_account_profile()'s Authorization header.

    A later ticket reuses this for a daemon-side backfill of legacy
    Profiles; kept here (not private) so that caller can import it too.
    """
    try:
        raw = secret_store.get_token(profile.id)
    except Exception:
        return None
    try:
        access_token = oauth_credential.decode(raw).access_token
    except Exception:
        return None
    if not access_token:
        return None
    try:
        return anthropic_oauth.fetch_account_profile(access_token)
    except Exception:
        return None


@dataclass(frozen=True)
class LegacyOAuthMatchResult:
    """What to do with a legacy (org_uuid=None) match from
    find_by_account_and_org(), decided from the EXISTING Profile's own
    stored credential rather than the incoming login — see
    resolve_legacy_oauth_match().

    `reuse_profile` is the Profile the caller should update in place
    (credential + org fields), or None when the caller must instead CREATE a
    new Profile for the incoming login and leave the existing one alone.

    `displaced_*` are set only when `reuse_profile` is None because the
    existing Profile's own identity was successfully resolved and turned out
    to be a DIFFERENT organization (never set on "couldn't resolve") — they
    carry what the existing Profile's own credential proved it really is
    (its own true org_uuid/org_name/organization_type), so a caller like
    cli.add_account() can tell the user what happened without a second
    lookup.
    """
    reuse_profile: Optional[Profile]
    displaced_profile: Optional[Profile] = None
    displaced_org_uuid: Optional[str] = None
    displaced_org_name: Optional[str] = None
    displaced_organization_type: Optional[str] = None


def resolve_legacy_oauth_match(existing: Optional[Profile], needs_backfill: bool,
                                org_uuid: Optional[str], organization_type: Optional[str],
                                *, allow_reuse_when_unresolved: bool = False) -> LegacyOAuthMatchResult:
    """The ONE place that decides what a legacy match from
    find_by_account_and_org() means. Shared by upsert_oauth_profile() and
    daemon.py's POST /api/profiles handler so this rule can never diverge
    between them — this codebase already had three separately-drifting
    copies of account dedup once (see find_by_account_and_org()'s docstring
    and this ticket's history); a login/credential rule is exactly the kind
    of logic that must live in one place.

    `existing`/`needs_backfill` are exactly what find_by_account_and_org()
    returned. When `needs_backfill` is False (no match, or an exact-pair
    match already reuses correctly on its own), `existing` is returned
    unchanged — there is nothing ambiguous here to resolve.

    When `needs_backfill` is True, this resolves `existing`'s own identity
    from ITS OWN stored credential (resolve_profile_own_identity() — never
    the incoming login, never a refresh) and decides:
      - resolves to the SAME org_uuid as the incoming login -> `existing`
        really is this account under this organization; returned unchanged
        so the caller reuses it (and backfills org_uuid/organization_type
        from the incoming login, same as it always did).
      - resolves to a DIFFERENT org_uuid -> `existing` is a different,
        already-identified subscription — the 2026-09-22 incident: a legacy
        Team Profile's credential silently overwritten by a personal-plan
        login that merely shared its account_uuid. `existing` is backfilled
        HERE with ITS OWN true org fields (never the incoming login's) —
        credential and name untouched — so it stops looking "legacy" the
        next time its real account re-authenticates, and this returns
        reuse_profile=None so the caller creates a SEPARATE new Profile for
        the incoming login instead of touching this one.
      - cannot be resolved at all (no stored token, decode error, network
        error, anything) -> the safe default is None (create a new Profile
        rather than guess which subscription `existing` really is) UNLESS
        `allow_reuse_when_unresolved` is True. Only cli.reauth() passes that
        opt-in, and only after ITS OWN interactive confirmation — reauth
        already knows exactly which Profile the user picked to re-auth, and
        owns getting the user's explicit go-ahead itself; every other caller
        keeps the safe default.
    """
    if not needs_backfill or existing is None:
        return LegacyOAuthMatchResult(reuse_profile=existing)

    resolved = resolve_profile_own_identity(existing)
    if resolved is not None:
        if resolved.org_uuid == org_uuid:
            return LegacyOAuthMatchResult(reuse_profile=existing)
        # A different, already-identified subscription: record ITS OWN true
        # org fields — never the incoming login's — leaving credential and
        # name untouched, then signal "create new" for the incoming login.
        update_profile(existing.id, org_uuid=resolved.org_uuid, organization_type=resolved.organization_type)
        return LegacyOAuthMatchResult(
            reuse_profile=None,
            displaced_profile=existing,
            displaced_org_uuid=resolved.org_uuid,
            displaced_org_name=resolved.org_name,
            displaced_organization_type=resolved.organization_type,
        )

    return LegacyOAuthMatchResult(reuse_profile=existing if allow_reuse_when_unresolved else None)


def update_credential(profile_id: str, credential: str, *, refresh_token: Optional[str] = None,
                       expires_at: Optional[int] = None) -> None:
    if not credential or len(credential.strip()) < 8:
        raise ValidationError("Credential looks too short — paste the complete token/key.")

    # refresh_token/expires_at are passed only for an OAuth credential that
    # came with them (CLI `add-account`, "Import current login"). A manually
    # pasted token or API key leaves both None, and encode() then stores a
    # plain string.
    stored = oauth_credential.encode(oauth_credential.StoredOAuthCredential(
        access_token=credential, refresh_token=refresh_token, expires_at=expires_at))
    secret_store.set_token(profile_id, stored)

    # Stamp credential_updated_at so the running daemon's Gateway — separate
    # in-memory state, possibly in another process — notices the fresh
    # credential and clears a stuck AUTH_INVALID state. recover_expired_
    # cooldowns only handles time-based recovery, not this.
    with CONFIG_LOCK:
        pool = load_pool()
        existing = pool.get(profile_id)
        name = existing.name if existing else profile_id
        if existing is not None:
            updated = replace(existing, credential_updated_at=datetime.now(timezone.utc).isoformat())
            pool.profiles = [updated if p.id == profile_id else p for p in pool.profiles]
            save_pool(pool)

    activity.record("config", f"{name} credential refreshed")


def _stamp_credential_updated_at(profile_id: str) -> None:
    """Shared by update_credential() and update_credential_raw(): both need
    the same Profile.credential_updated_at signal that gateway.py's
    _sync_snapshot reads, whichever module owns the encoding."""
    with CONFIG_LOCK:
        pool = load_pool()
        existing = pool.get(profile_id)
        if existing is not None:
            updated = replace(existing, credential_updated_at=datetime.now(timezone.utc).isoformat())
            pool.profiles = [updated if p.id == profile_id else p for p in pool.profiles]
            save_pool(pool)


def update_credential_raw(profile_id: str, encoded_blob: str) -> None:
    """Stores an ALREADY-encoded credential blob as-is, for a kind whose own
    module owns the encoding (openai_credential.py for a codex Profile).
    update_credential() instead re-encodes through oauth_credential.py's
    Anthropic-specific shape.

    Skips update_credential()'s length check: a JSON blob's length says
    nothing about the credential inside it."""
    secret_store.set_token(profile_id, encoded_blob)
    _stamp_credential_updated_at(profile_id)


def create_profile(
    *,
    name: str,
    kind: str,
    credential: str,
    base_url: Optional[str] = None,
    auth_mode: str = "api_key",
    priority: Optional[int] = None,
    switch_threshold: float = 98.0,
    # Defaults to True because no add-profile flow exposes a way to turn it
    # on, so a False default would leave a new Profile permanently invisible
    # to Rotation. Pass False only for a deliberate manual-pin-only account.
    automatic: bool = True,
    default_model: Optional[str] = None,
    monthly_budget_cap: Optional[float] = None,
    token_threshold: Optional[int] = None,
    tag_color: Optional[str] = None,
    account_uuid: Optional[str] = None,
    org_uuid: Optional[str] = None,
    organization_type: Optional[str] = None,
    plan: Optional[str] = None,
    refresh_token: Optional[str] = None,
    expires_at: Optional[int] = None,
    claude_config_dir: Optional[str] = None,
    codex_home: Optional[str] = None,
    codex_model: Optional[str] = None,
    codex_reasoning_effort: Optional[str] = None,
    credential_already_encoded: bool = False,
) -> Profile:
    _validate(name, kind, base_url, auth_mode, tag_color)
    # Same reachable-from-HTTP surface as update_profile: POST /api/profiles
    # hands this a decoded JSON body. `priority` is excluded because None is
    # legitimate here and means "compute the next free slot" just below.
    _validate_field_types(
        switch_threshold=switch_threshold, token_threshold=token_threshold,
        monthly_budget_cap=monthly_budget_cap, automatic=automatic,
        default_model=default_model, tag_color=tag_color, plan=plan,
        codex_model=codex_model, codex_reasoning_effort=codex_reasoning_effort,
        claude_config_dir=claude_config_dir, codex_home=codex_home,
    )
    if priority is not None:
        _validate_field_types(priority=priority)
    if not credential or len(credential.strip()) < 8:
        raise ValidationError("Credential looks too short — paste the complete token/key.")
    if priority is None:
        # Slot a new Profile in after the existing ones. Without this, a
        # caller that computes no priority would tie with whatever already
        # held the top slot. The Dashboard's Add-Profile form computes the
        # same next-free-slot value client-side.
        existing = load_pool().profiles
        priority = max((p.priority for p in existing), default=0) + 1
    if kind == "oauth" and not account_uuid:
        raise ValidationError(
            "OAuth profiles need account_uuid — it's what lets the proxy rewrite "
            "metadata.user_id so Anthropic accepts the swapped-in credential. See "
            "proxy.py's module docstring for how it's used."
        )

    profile = Profile(
        id=_new_id(),
        name=name.strip(),
        kind=kind,
        base_url=base_url,
        auth_mode=auth_mode,
        priority=priority,
        switch_threshold=switch_threshold,
        automatic=automatic,
        default_model=default_model,
        monthly_budget_cap=monthly_budget_cap,
        token_threshold=token_threshold,
        tag_color=tag_color,
        account_uuid=account_uuid,
        org_uuid=org_uuid,
        organization_type=organization_type,
        plan=plan,
        claude_config_dir=claude_config_dir,
        codex_home=codex_home,
        codex_model=codex_model,
        codex_reasoning_effort=codex_reasoning_effort,
    )

    if credential_already_encoded:
        # A codex-kind (chatgpt_subscription) credential, already encoded by
        # openai_credential.encode(). That is not the Anthropic-shaped
        # oauth_credential blob below; re-encoding it would wrap a JSON
        # string inside another one and break every future decode.
        stored_credential = credential
    else:
        # refresh_token/expires_at are passed only for an OAuth credential
        # that came with them (CLI `add-account`, "Import current login"). A
        # manually pasted token leaves both None and is stored as a plain
        # string.
        stored_credential = oauth_credential.encode(oauth_credential.StoredOAuthCredential(
            access_token=credential, refresh_token=refresh_token, expires_at=expires_at))

    # Keychain first, config second. If the process dies between the two,
    # an orphaned Keychain entry with no config row is harmless, whereas a
    # config row pointing at a credential that was never written would let
    # the Router select a Profile with no secret behind it.
    try:
        secret_store.set_token(profile.id, stored_credential)
    except Exception as exc:
        raise ProfileRepositoryError(f"Could not store credential in Keychain: {exc}") from exc

    with CONFIG_LOCK:
        pool = load_pool()
        pool.profiles.append(profile)
        try:
            save_pool(pool)
        except Exception as exc:
            secret_store.delete_token(profile.id)
            raise ProfileRepositoryError(f"Could not save profile metadata, rolled back Keychain entry: {exc}") from exc

    activity.record("config", f"{profile.name} added", meta=f"kind={profile.kind}")
    return profile


def upsert_oauth_profile(*, name: str, account_uuid: str, credential: str, plan: Optional[str] = None,
                          org_uuid: Optional[str] = None, organization_type: Optional[str] = None,
                          refresh_token: Optional[str] = None, expires_at: Optional[int] = None,
                          claude_config_dir: Optional[str] = None,
                          allow_legacy_reuse_when_unresolved: bool = False) -> tuple[Profile, bool]:
    """The single dedup-and-upsert path every OAuth-adding flow shares (CLI
    `add-account`, `reauth`, "Import current login"): create a new Profile
    for this (account_uuid, org_uuid) pair, or, if one is already
    registered, refresh its credential and keep its plan, org_uuid,
    organization_type and claude_config_dir current.

    Dedups on the PAIR (account_uuid, org_uuid), not on account_uuid alone —
    see find_by_account_and_org() for why account_uuid alone is not a unique
    identity. A Profile registered before org_uuid existed (org_uuid=None) is
    a LEGACY match: rather than trusting this login's org_uuid to decide what
    that pre-existing Profile is (the 2026-09-22 incident — see
    resolve_legacy_oauth_match()), it is resolved from its OWN stored
    credential first. Only a Profile that is PROVEN to be this same
    organization gets its org_uuid/organization_type backfilled and its
    credential refreshed; anything else (a different organization, or a
    Profile whose own credential can't be resolved) gets a separate new
    Profile instead, leaving the existing one's credential and name
    untouched.

    `allow_legacy_reuse_when_unresolved` is the ONE escape hatch from that
    "can't resolve -> create new" default: it lets a legacy Profile whose own
    credential could not be resolved be reused anyway. Defaults to False so
    no existing caller can reach it by accident — only cli.reauth() passes
    True, and only after its own interactive confirmation naming the
    organization about to be recorded.

    Returns (profile, reused). reused=True means an existing Profile was
    refreshed in place, not that anything new was added — this is always
    False when a new Profile was created, including on the legacy-mismatch
    path above."""
    existing, needs_backfill = find_by_account_and_org(account_uuid, org_uuid)
    match = resolve_legacy_oauth_match(
        existing, needs_backfill, org_uuid, organization_type,
        allow_reuse_when_unresolved=allow_legacy_reuse_when_unresolved)
    existing = match.reuse_profile
    if existing is not None:
        update_credential(existing.id, credential, refresh_token=refresh_token, expires_at=expires_at)
        changes = {k: v for k, v in {
            "plan": plan, "org_uuid": org_uuid, "organization_type": organization_type,
            "claude_config_dir": claude_config_dir,
        }.items() if v is not None}
        updated = update_profile(existing.id, **changes) if changes else existing
        return updated, True

    profile = create_profile(name=name, kind="oauth", credential=credential, account_uuid=account_uuid,
                              org_uuid=org_uuid, organization_type=organization_type,
                              plan=plan, refresh_token=refresh_token, expires_at=expires_at,
                              claude_config_dir=claude_config_dir)
    return profile, False


def upsert_codex_profile(*, name: str, account_id: str, encoded_credential: str,
                          plan: Optional[str] = None, codex_home: Optional[str] = None) -> tuple[Profile, bool]:
    """The codex-kind analogue of upsert_oauth_profile(): same
    create-or-refresh-in-place shape, with the OpenAI account_id as the
    dedup key. Profile.account_uuid doubles as a generic upstream-account
    identity slot — despite the name, find_by_account_uuid() is a plain
    equality match with no kind-specific meaning.

    encoded_credential must already be openai_credential.encode()'s output;
    this function stores it as-is rather than building the blob."""
    existing = find_by_account_uuid(account_id)
    if existing is not None:
        update_credential_raw(existing.id, encoded_credential)
        changes = {k: v for k, v in {"plan": plan, "codex_home": codex_home}.items() if v is not None}
        updated = update_profile(existing.id, **changes) if changes else existing
        return updated, True

    profile = create_profile(name=name, kind="codex", credential=encoded_credential,
                              auth_mode="chatgpt_subscription", account_uuid=account_id,
                              plan=plan, codex_home=codex_home, credential_already_encoded=True)
    return profile, False


def update_profile(profile_id: str, **changes) -> Profile:
    allowed = {
        "name", "priority", "switch_threshold", "enabled", "automatic",
        "default_model", "monthly_budget_cap", "token_threshold", "tag_color", "base_url", "auth_mode", "plan",
        "org_uuid", "organization_type",
        "claude_config_dir", "codex_home", "codex_model", "codex_reasoning_effort",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValidationError(f"Cannot change fields: {sorted(unknown)}")
    _validate_field_types(**changes)

    with CONFIG_LOCK:
        pool = load_pool()
        existing = pool.get(profile_id)
        if existing is None:
            raise ProfileRepositoryError(f"No profile with id {profile_id!r}.")

        updated = replace(existing, **changes)
        _validate(updated.name, updated.kind, updated.base_url, updated.auth_mode, updated.tag_color)

        pool.profiles = [updated if p.id == profile_id else p for p in pool.profiles]
        save_pool(pool)

    changed = ", ".join(f"{k}={v}" for k, v in changes.items())
    activity.record("config", f"{updated.name} updated", meta=changed)
    return updated


def _renumber_priorities_sequentially(profiles: list[Profile]) -> list[Profile]:
    """Reassigns 1..N with no gaps, preserving each Profile's relative rank
    via a stable sort on the old priority. Only `.priority` changes; the
    list's own order is untouched.

    Without this, deleting a non-last Profile leaves a gap (1,2,3,5) that
    create_profile()'s max(priorities)+1 never reclaims."""
    by_old_priority = sorted(profiles, key=lambda p: p.priority)
    new_priority_by_id = {p.id: rank for rank, p in enumerate(by_old_priority, start=1)}
    return [replace(p, priority=new_priority_by_id[p.id]) for p in profiles]


def delete_profile(profile_id: str) -> None:
    with CONFIG_LOCK:
        pool = load_pool()
        existing = pool.get(profile_id)
        if existing is None:
            raise ProfileRepositoryError(f"No profile with id {profile_id!r}.")
        pool.profiles = _renumber_priorities_sequentially(
            [p for p in pool.profiles if p.id != profile_id])
        save_pool(pool)
    # Credential deleted last: if the process dies right after save_pool,
    # an orphaned Keychain entry is harmless, whereas a config row
    # referencing a missing secret is not.
    secret_store.delete_token(profile_id)
    _remove_isolated_login_artifacts(existing)
    activity.record("config", f"{existing.name} removed")


def _remove_isolated_login_artifacts(p: Profile) -> None:
    """Removes the per-account login directories a Profile owned, and the
    Keychain entry Claude Code wrote inside its isolated one.

    Both `add-account` and `add-codex-account` create a directory holding a
    LIVE refresh token — `.credentials.json` (or a directory-derived Keychain
    entry) for Claude, `auth.json` for Codex. Only `codex_home` used to be
    cleaned up here, so removing a Claude account from the Dashboard left its
    refresh token on disk and in the Keychain with nothing referencing it. Only
    `purge` cleaned those, which is not where most accounts get removed.

    Best-effort throughout: the Profile is already gone from config by this
    point, and failing to tidy up must not turn a completed delete into an
    error. Both paths are validated to live under this tool's own accounts
    roots when they are set (see _validate_isolated_dir)."""
    import shutil as _shutil

    for directory in (p.codex_home, p.claude_config_dir):
        if directory:
            _shutil.rmtree(directory, ignore_errors=True)
    if p.claude_config_dir:
        try:
            from . import anthropic_oauth
            anthropic_oauth.remove_isolated_logins([p.claude_config_dir])
        except Exception:
            pass


def reset_all_profiles() -> int:
    """The Settings page's danger-zone action. Deletes every Profile's
    Keychain credential and clears the Pool. Never touches
    shared_claude_dir or ~/.claude."""
    with CONFIG_LOCK:
        pool = load_pool()
        count = len(pool.profiles)
        removed = list(pool.profiles)
        for p in pool.profiles:
            secret_store.delete_token(p.id)
        pool.profiles = []
        save_pool(pool)
    # Same cleanup delete_profile does, for the same reason: each isolated
    # login directory holds a live refresh token, and "remove everything"
    # that leaves credentials behind has not removed everything.
    for p in removed:
        _remove_isolated_login_artifacts(p)
    activity.record("config", f"All {count} profile(s) removed (reset)")
    return count
