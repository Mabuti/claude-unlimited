"""Export/Import bundles.

`cryptography` is the single exception to the dependency-free-backend rule,
and its use is confined to this file. A bundle containing Profiles (and
therefore credentials) is ALWAYS passphrase-encrypted; Settings- or
Activity-only bundles are plain JSON, since they hold no secrets.

Two-step by design: import_bundle() only parses and never writes, so the
caller can preview the result and then call apply_import() explicitly. A bad
passphrase or a stale file therefore cannot silently mutate local state.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import activity as activity_module
from . import profiles as profile_repo
from . import secret_store
from .config import CONFIG_LOCK, Profile, load_pool, normalize_forced_subagents, save_pool

BUNDLE_VERSION = 1
PBKDF2_ITERATIONS = 600_000  # OWASP 2023 recommendation floor for PBKDF2-HMAC-SHA256


# Settings that describe THIS machine and never travel in a bundle, in
# either direction.
#   port      — a bundle that moved it would silently relocate the dashboard
#               on whatever box imported it, with no way to know the new port
#               is free. Changing it is POST /api/settings/port, which
#               preflights the bind first.
#   launchers — the command `cu code` / `cu codex` execute, with the pool's
#               bearer in the environment. Imported, it would let whoever wrote
#               a bundle run code as the importer; exported, it would hand this
#               machine's flags (e.g. --dangerously-skip-permissions) to a
#               teammate without the import preview ever showing them.
_MACHINE_LOCAL_SETTINGS = frozenset({"port", "launchers"})


class ExportImportError(RuntimeError):
    pass


class WrongPassphraseError(ExportImportError):
    pass


def _derive_key(passphrase: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


@dataclass(frozen=True)
class ExportedProfile:
    name: str
    kind: str
    base_url: Optional[str]
    auth_mode: str
    priority: int
    switch_threshold: float
    enabled: bool
    automatic: bool
    default_model: Optional[str]
    force_model: Optional[str]
    monthly_budget_cap: Optional[float]
    token_threshold: Optional[int]
    tag_color: Optional[str]
    account_uuid: Optional[str]
    credential: str  # the one place a secret is ever put in a serializable structure
    # The account's tier ("max"/"pro"), which only the add-account and
    # import-login flows ever discover. Nothing recomputes it for an oauth
    # Profile afterwards, so leaving it out of the bundle meant a restored
    # account showed no tier in the Dashboard forever.
    plan: Optional[str] = None
    # org_uuid/organization_type: the other half of the identity pair
    # alongside account_uuid (see profiles.find_by_account_and_org) and the
    # source of the plan label. Without these a restored bundle can only
    # ever legacy-match on account_uuid alone, which is exactly the
    # ambiguity this pair exists to resolve.
    org_uuid: Optional[str] = None
    organization_type: Optional[str] = None
    # codex_home is deliberately NOT exported: it is a local path from the
    # one-time `codex login` handshake, meaningless on another machine, and
    # the credential that a re-import actually needs is already carried
    # above.
    codex_model: Optional[str] = None
    codex_reasoning_effort: Optional[str] = None
    # codex_user_id IS exported, unlike codex_home: it's identity (the other
    # half of a codex Profile's dedup pair alongside account_uuid — see
    # profiles.find_codex_profile), not a local path. Without it, a bundle
    # item could only ever legacy-match an existing codex Profile on
    # account_uuid alone, which is exactly the 2026-09-22 incident's
    # ambiguity on the import side too.
    codex_user_id: Optional[str] = None
    # "Every subagent goes here". Defaulted so bundles written before the
    # field existed still import.
    forced_for_subagents: bool = False
    # "Leave when Fable is spent". Same reason for the default.
    leave_on_fable_limit: bool = False


def build_export_bundle(
    *,
    include_profiles: bool,
    include_settings: bool,
    include_activity: bool,
    passphrase: Optional[str] = None,
) -> bytes:
    if include_profiles and not passphrase:
        raise ExportImportError("A passphrase is required to export Profiles (credentials never leave in the clear).")

    pool = load_pool()
    payload: dict = {"bundle_version": BUNDLE_VERSION, "exported_at": datetime.now(timezone.utc).isoformat()}

    if include_profiles:
        exported = []
        for p in pool.profiles:
            try:
                cred = secret_store.get_token(p.id)
            except Exception as exc:
                raise ExportImportError(f"Could not read credential for {p.name!r} from Keychain: {exc}") from exc
            exported.append(asdict(ExportedProfile(
                name=p.name, kind=p.kind, base_url=p.base_url, auth_mode=p.auth_mode,
                priority=p.priority, switch_threshold=p.switch_threshold, enabled=p.enabled,
                automatic=p.automatic, default_model=p.default_model, force_model=p.force_model,
                monthly_budget_cap=p.monthly_budget_cap,
                token_threshold=p.token_threshold,
                tag_color=p.tag_color, account_uuid=p.account_uuid, credential=cred,
                plan=p.plan, org_uuid=p.org_uuid, organization_type=p.organization_type,
                codex_model=p.codex_model, codex_reasoning_effort=p.codex_reasoning_effort,
                codex_user_id=p.codex_user_id,
                forced_for_subagents=p.forced_for_subagents,
                leave_on_fable_limit=p.leave_on_fable_limit,
            )))
        payload["profiles"] = exported

    if include_settings:
        # _MACHINE_LOCAL_SETTINGS are withheld — see there for why.
        payload["settings"] = {k: v for k, v in asdict(pool.settings).items()
                               if k not in _MACHINE_LOCAL_SETTINGS}

    if include_activity:
        payload["activity"] = [asdict(e) for e in activity_module.list_events(limit=activity_module.MAX_EVENTS)]

    if "profiles" in payload:
        # Once a passphrase is required at all, the whole payload is sealed
        # together, so settings and activity can't be read out of an
        # unencrypted sibling section.
        plaintext = json.dumps(payload).encode("utf-8")
        salt = os.urandom(16)
        key = _derive_key(passphrase, salt)
        ciphertext = Fernet(key).encrypt(plaintext)
        envelope = {
            "bundle_version": BUNDLE_VERSION,
            "encrypted": True,
            "kdf": "pbkdf2-sha256",
            "iterations": PBKDF2_ITERATIONS,
            "salt": base64.b64encode(salt).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        return json.dumps(envelope, indent=2).encode("utf-8")

    envelope = {"bundle_version": BUNDLE_VERSION, "encrypted": False, "data": payload}
    return json.dumps(envelope, indent=2).encode("utf-8")


@dataclass(frozen=True)
class ParsedBundle:
    profiles: list  # list[ExportedProfile]-shaped dicts, or [] if not included
    settings: Optional[dict]
    activity: Optional[list]


def import_bundle(data: bytes, passphrase: Optional[str] = None) -> ParsedBundle:
    """Read-only: decrypts and parses, never writes. Raises
    WrongPassphraseError specifically, so the caller can show a clear
    message rather than a generic parse failure."""

    try:
        envelope = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ExportImportError(f"Not a valid export bundle: {exc}") from exc

    if envelope.get("bundle_version") != BUNDLE_VERSION:
        raise ExportImportError(f"Unsupported bundle version {envelope.get('bundle_version')!r}.")

    if envelope.get("encrypted"):
        if not passphrase:
            raise ExportImportError("This bundle is encrypted — a passphrase is required.")
        try:
            salt = base64.b64decode(envelope["salt"])
            ciphertext = base64.b64decode(envelope["ciphertext"])
            key = _derive_key(passphrase, salt, int(envelope.get("iterations", PBKDF2_ITERATIONS)))
            plaintext = Fernet(key).decrypt(ciphertext)
        except InvalidToken as exc:
            raise WrongPassphraseError("Wrong passphrase, or the file is corrupted.") from exc
        except (KeyError, ValueError) as exc:
            raise ExportImportError(f"Malformed encrypted bundle: {exc}") from exc
        payload = json.loads(plaintext)
    else:
        payload = envelope.get("data", {})

    return ParsedBundle(
        profiles=payload.get("profiles", []),
        settings=payload.get("settings"),
        activity=payload.get("activity"),
    )


def apply_import(
    parsed: ParsedBundle,
    *,
    import_profiles: bool,
    import_settings: bool,
    conflict_strategy: str = "keep_existing",  # "keep_existing" | "use_imported"
) -> dict:
    """Writes. Call only after `parsed` has been previewed and confirmed.

    Never touches Activity: a machine's history is its own, and importing
    another machine's activity log would be meaningless.

    An oauth item that conflicts with an existing Profile is decided by
    profiles.resolve_legacy_oauth_match() — the same function daemon.py's
    POST /api/profiles and profiles.upsert_oauth_profile() use — never by
    matching on its own here. See that function and
    profiles.find_by_account_and_org() for what a match means and why an
    unknown organization (on either side) is never enough on its own to
    reuse a credential.

    A codex item is decided the same way by profiles.find_codex_profile()
    — the same function upsert_codex_profile() uses — with one difference
    from the oauth path: on a "not confirmed" outcome there, `existing` is
    handed back non-None (needs_backfill=True) so resolve_legacy_oauth_match()
    can still run; find_codex_profile() instead hands back (None, True)
    directly, since there is no equivalent resolution function to route
    through (no network call can settle a ChatGPT identity — it's pure
    local decode). Either way the result for an item conflict_strategy
    asked to apply but couldn't be proven safe is the same:
    profiles_overwrite_blocked, and the existing Profile is left alone."""

    if conflict_strategy not in ("keep_existing", "use_imported"):
        raise ExportImportError(f"Unknown conflict_strategy {conflict_strategy!r}.")

    result = {
        "profiles_added": 0, "profiles_updated": 0, "profiles_skipped": 0,
        # Distinct from profiles_skipped (conflict_strategy="keep_existing",
        # a deliberate no-op): this counts an item the caller DID ask to
        # apply ("use_imported") that profiles.resolve_legacy_oauth_match()
        # refused, because the item's organization couldn't be proven to be
        # the existing Profile's own. Surfaced separately so the caller and
        # the dashboard can tell "nothing to do here" apart from "something
        # was held back for safety".
        "profiles_overwrite_blocked": 0,
        "settings_applied": False,
    }

    if import_profiles:
        # Each item is looked up, decided and (if applicable) written as its
        # own transaction, rather than one CONFIG_LOCK spanning the whole
        # bundle: an oauth item's decision can itself need to write — on a
        # legacy match that resolves to a DIFFERENT organization,
        # resolve_legacy_oauth_match() backfills the EXISTING Profile's own
        # org fields via profiles.update_profile(), which takes CONFIG_LOCK
        # itself. CONFIG_LOCK is a plain, non-reentrant Lock, so calling
        # that while already holding it here would deadlock. A fresh
        # load_pool() per item still sees every earlier item's already-saved
        # effect, so cross-item matching within one bundle is unaffected.
        for item in parsed.profiles:
            item_uuid = item.get("account_uuid")
            item_org_uuid = item.get("org_uuid")
            item_organization_type = item.get("organization_type")
            item_kind = item.get("kind")
            item_codex_user_id = item.get("codex_user_id")
            if item_kind == "codex" and item.get("credential"):
                # Never decide a codex item's identity from the bundle's
                # claimed codex_user_id field while holding the credential
                # that would actually prove it — same rule as the oauth
                # branch's "resolve from the incoming credential before
                # trusting an unknown org" below, and the reason
                # upsert_codex_profile() resolves the incoming login's own
                # id_token instead of taking a caller's word for who it is.
                # The credential wins when it resolves to anything at all;
                # the bundle's own field is only a fallback for when the
                # credential itself yields no user id — an id_token with no
                # chatgpt_user_id claim, or no id_token at all (a raw,
                # non-JSON credential shape) — where the field is genuinely
                # all there is to go on. That includes, but isn't limited
                # to, an old-shape item exported before codex_user_id
                # existed: such an item has no field to fall back to
                # either, so this simply leaves item_codex_user_id at
                # item.get("codex_user_id")'s default of None in that case.
                from_credential = profile_repo.codex_user_id_from_encoded_credential(item["credential"])
                if from_credential is not None:
                    item_codex_user_id = from_credential
            existing = None
            needs_backfill = False
            # codex_blocked is find_codex_profile()'s `blocked` return: true
            # only when an UNRESOLVABLE same-account_id candidate stands
            # between this account_id and a safe decision, or the incoming
            # identity itself is unknown — see its docstring's Returns
            # section. It is NOT true just because every candidate resolved
            # and none of them matched: that is a genuinely distinct person
            # sharing this account_id (the 2026-09-22 incident's own
            # shape), and existing=None, codex_blocked=False there falls
            # straight through to the ordinary "add a new Profile" path
            # below, same as a brand-new account_id would. It has no oauth
            # equivalent: find_by_account_and_org() always hands back a
            # candidate (needs_backfill=True) for its ambiguous case
            # instead, which is what routes an oauth item through
            # resolve_legacy_oauth_match() below rather than needing a
            # separate flag here.
            codex_blocked = False
            if item_uuid:
                if item_kind == "codex":
                    existing, codex_blocked = profile_repo.find_codex_profile(item_uuid, item_codex_user_id)
                else:
                    existing, needs_backfill = profile_repo.find_by_account_and_org(item_uuid, item_org_uuid)

            if (existing is not None or codex_blocked) and conflict_strategy == "keep_existing":
                # A conflict exists (confirmed match, or an ambiguous
                # same-account_id candidate) and the caller asked to keep
                # what's already there — never even attempt resolution, the
                # same short-circuit the oauth branch below takes when
                # `existing` alone was enough to answer this.
                result["profiles_skipped"] += 1
                continue

            if existing is None and codex_blocked:
                # Never safe to overwrite, and — unlike the genuine "no
                # conflict" case (no candidate at all, or every candidate
                # resolved and none of them is this item's chatgpt_user_id)
                # — never safe to add a new Profile for this item either:
                # this account_id has at least one Profile whose OWN
                # identity couldn't be resolved at all, so there's no way
                # to rule out that THAT Profile is actually this item's
                # person. A caller that asked to apply this item gets
                # nothing done, exactly mirroring the oauth "not safe to
                # overwrite" branch just below. See
                # profiles.find_codex_profile()'s docstring for why
                # upsert_codex_profile()'s own CLI path is allowed to
                # create a new Profile on this same outcome and this import
                # path is not: a bundle apply is never a fresh interactive
                # login.
                result["profiles_overwrite_blocked"] += 1
                continue

            if existing is not None and item_kind == "oauth":
                # Route through the ONE decision function rather than
                # trusting find_by_account_and_org()'s match on its own —
                # this is Finding 2: apply_import() used to call
                # secret_store.set_token() straight off that match, so a
                # bundle written before organizations existed could silently
                # overwrite a different organization's credential exactly
                # like the 2026-09-22 incident. The bundle carries this
                # item's own credential, so when its organization is
                # unknown, resolve it from that credential first — same
                # "never decide on an unknown org while holding the
                # credential that would reveal it" rule as daemon.py's POST
                # /api/profiles.
                if item_org_uuid is None and item.get("credential"):
                    resolved_incoming = profile_repo.resolve_identity_from_encoded_credential(item["credential"])
                    if resolved_incoming is not None:
                        item_org_uuid = resolved_incoming.org_uuid
                        item_organization_type = resolved_incoming.organization_type
                        # Re-look-up now that the org is actually known: an
                        # exact pair match is unambiguous and reuses without
                        # ever going through resolve_legacy_oauth_match().
                        existing, needs_backfill = profile_repo.find_by_account_and_org(item_uuid, item_org_uuid)
                match = profile_repo.resolve_legacy_oauth_match(
                    existing, needs_backfill, item_org_uuid, item_organization_type)
                existing = match.reuse_profile
                if existing is None:
                    # Not safe to overwrite. Leave the existing Profile's
                    # credential and name exactly as they are — do NOT fall
                    # through to creating a new Profile either: the caller
                    # asked to update a specific conflicting Profile, not to
                    # add a new row, so the safest response to "can't prove
                    # this is safe" is to do nothing at all with this item.
                    result["profiles_overwrite_blocked"] += 1
                    continue

            if existing is not None:
                # "Use imported version" means the whole Profile, not only
                # its credential: every bundle field below is applied, not
                # just the stored token.
                secret_store.set_token(existing.id, item["credential"])
                with CONFIG_LOCK:
                    pool = load_pool()
                    updated = replace(
                        existing, name=item["name"], base_url=item.get("base_url"),
                        auth_mode=item.get("auth_mode", "api_key"), priority=item.get("priority", 1),
                        switch_threshold=item.get("switch_threshold", 98.0), enabled=item.get("enabled", True),
                        automatic=item.get("automatic", True), default_model=item.get("default_model"),
                        force_model=item.get("force_model"),
                        monthly_budget_cap=item.get("monthly_budget_cap"), token_threshold=item.get("token_threshold"),
                        tag_color=item.get("tag_color"), plan=item.get("plan"),
                        org_uuid=item_org_uuid, organization_type=item_organization_type,
                        codex_model=item.get("codex_model"), codex_reasoning_effort=item.get("codex_reasoning_effort"),
                        codex_user_id=item_codex_user_id,
                        forced_for_subagents=bool(item.get("forced_for_subagents", False)),
                        leave_on_fable_limit=item.get("leave_on_fable_limit") is True,
                    )
                    pool.profiles = [updated if p.id == existing.id else p for p in pool.profiles]
                    # A bundle can bring a forced-subagent Profile into a pool
                    # that already has one; the holder already routing
                    # (earlier in the list) keeps it, rather than save_pool()
                    # rejecting the import.
                    pool.profiles = normalize_forced_subagents(pool.profiles)
                    save_pool(pool)
                result["profiles_updated"] += 1
                continue

            import secrets as _secrets

            new_profile = Profile(
                id=_secrets.token_hex(8), name=item["name"], kind=item["kind"], base_url=item.get("base_url"),
                auth_mode=item.get("auth_mode", "api_key"), priority=item.get("priority", 1),
                switch_threshold=item.get("switch_threshold", 98.0), enabled=item.get("enabled", True),
                automatic=item.get("automatic", True), default_model=item.get("default_model"),
                force_model=item.get("force_model"),
                monthly_budget_cap=item.get("monthly_budget_cap"), token_threshold=item.get("token_threshold"),
                tag_color=item.get("tag_color"),
                account_uuid=item.get("account_uuid"), plan=item.get("plan"),
                org_uuid=item_org_uuid, organization_type=item_organization_type,
                codex_model=item.get("codex_model"), codex_reasoning_effort=item.get("codex_reasoning_effort"),
                codex_user_id=item_codex_user_id,
                forced_for_subagents=bool(item.get("forced_for_subagents", False)),
                leave_on_fable_limit=item.get("leave_on_fable_limit") is True,
            )
            secret_store.set_token(new_profile.id, item["credential"])
            with CONFIG_LOCK:
                pool = load_pool()
                pool.profiles.append(new_profile)
                # A bundle can bring a forced-subagent Profile into a pool
                # that already has one; the holder already routing (earlier
                # in the list) keeps it, rather than save_pool() rejecting
                # the import.
                pool.profiles = normalize_forced_subagents(pool.profiles)
                save_pool(pool)
            result["profiles_added"] += 1

    if import_settings and parsed.settings:
        from .config import Settings, validated_settings_changes

        # Validated, not trusted. A bundle is a file from somewhere else, and
        # settings now carry decisions with real consequences — model_parity
        # picks which model every codex request runs on, which is what Codex
        # quota is spent on (docs/adr/0007). Importing that unchecked would let
        # a bundle silently change someone's spending.
        # _MACHINE_LOCAL_SETTINGS are dropped rather than rejected: a bundle
        # written by hand, or by a build that still exported them, must still
        # import its other settings instead of 400-ing on a key the user never
        # chose to send.
        incoming = {k: v for k, v in parsed.settings.items()
                    if k in Settings.__dataclass_fields__ and k not in _MACHINE_LOCAL_SETTINGS}
        incoming = validated_settings_changes(incoming)

        with CONFIG_LOCK:
            pool = load_pool()
            # Merged onto the current settings, not built fresh from the
            # bundle. Settings(**incoming) reverted every field the bundle did
            # not mention back to its dataclass default — and a bundle exported
            # by a version that predates a field simply has no key for it. So
            # importing an older bundle silently reset the language and
            # notification preferences of whoever imported it. This is also
            # what update_settings() does, and the two should not disagree
            # about what writing settings means.
            pool.settings = replace(pool.settings, **incoming)
            save_pool(pool)
        result["settings_applied"] = True

    activity_module.record("config", "Imported an export bundle", meta=str(result))
    return result
