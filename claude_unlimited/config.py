from __future__ import annotations

import json
import os
import shlex
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import List, Optional

APP_DIR = Path.home() / ".claude-unlimited"
CONFIG_FILE = APP_DIR / "config.json"

# The isolated per-account login directories. Defined here, once, because two
# modules need to agree on them: cli.py CREATES them during `add-account` /
# `add-codex-account`, and profiles.py REFUSES any Profile naming a directory
# outside them — delete_profile() removes codex_home recursively, so an
# unconstrained value is a request to delete a directory of someone's
# choosing. Two independent derivations of the same path would let those two
# disagree, which is exactly how the check would end up validating nothing.
_CLAUDE_ACCOUNTS_LEAF = "claude-accounts"
_CODEX_ACCOUNTS_LEAF = "codex-accounts"
CLAUDE_ACCOUNTS_DIR = APP_DIR / _CLAUDE_ACCOUNTS_LEAF
CODEX_ACCOUNTS_DIR = APP_DIR / _CODEX_ACCOUNTS_LEAF


def accounts_roots() -> tuple:
    """(claude, codex) resolved against APP_DIR **at call time**.

    The constants above are bound at import, which is what cli.py wants. The
    validator wants the live value instead, so that redirecting APP_DIR — as
    every test does — redirects what it will accept too. Otherwise the check
    would validate against the real home directory during a test run, which
    is both wrong and the sort of thing that quietly starts touching real
    files."""
    return (APP_DIR / _CLAUDE_ACCOUNTS_LEAF, APP_DIR / _CODEX_ACCOUNTS_LEAF)

# Guards the whole load_pool() -> mutate -> save_pool() cycle at the call
# sites (profiles.py), not just the I/O inside this module. The daemon runs
# one thread per connection, so without it two concurrent writes to
# /api/profiles/* can both load the same old state and drop one change, and
# their save_pool() calls can collide on the shared ".json.tmp" path and
# raise FileNotFoundError out of tmp.replace().
CONFIG_LOCK = threading.Lock()

DEFAULT_SWITCH_THRESHOLD = 98.0

# The one definition of what counts as a usable listen port, for every path
# that resolves one: the --port flag, CLAUDE_UNLIMITED_PORT, settings.port and
# POST /api/settings/port (daemon.py imports these rather than repeating the
# numbers, so the CLI and the Dashboard can never disagree about what they
# accept). The floor is 1024, not 1: this daemon does not run privileged, so
# anything below that cannot be bound and telling the user "between 1024 and
# 65535" up front beats an EACCES from deep inside the bind.
MIN_PORT = 1024
MAX_PORT = 65535


def port_in_range(port: int) -> bool:
    """True when `port` is one this daemon could actually bind."""
    return MIN_PORT <= port <= MAX_PORT


def _default_port() -> int:
    """daemon.py owns DEFAULT_PORT (4317) and already imports this module at
    its own top level, so importing daemon back at config.py's top level
    would be circular — daemon.py's `from .config import ...` would run
    against a config module that hasn't finished defining itself yet.
    Deferred to call time, this only runs once daemon.py exists to import
    from, whichever module happened to be imported first."""
    from .daemon import DEFAULT_PORT

    return DEFAULT_PORT


@dataclass(frozen=True)
class LauncherKind:
    """One entry in LAUNCHER_KINDS: everything the config layer and the
    dashboard need to know about a downstream CLI without hardcoding its
    name anywhere else."""

    kind: str
    label_key: str  # locales/*.json key for the dashboard's row label
    default_command: str  # what launch_argv() falls back to when unconfigured
    used_by: str  # the claude-unlimited command that shells out to it


# The single registry of downstream CLIs (plan configurable-launchers-and-
# port.plan.md §3.1). Adding a future one is one entry here plus one locale
# key — never a new hardcoded name at a call site.
LAUNCHER_KINDS = (
    LauncherKind(kind="claude", label_key="settings.launchers.claude",
                 default_command="claude", used_by="claude-unlimited code"),
    LauncherKind(kind="codex", label_key="settings.launchers.codex",
                 default_command="codex", used_by="claude-unlimited add-codex-account"),
)

_LAUNCHER_KINDS_BY_NAME = {lk.kind: lk for lk in LAUNCHER_KINDS}


def _split_command(text: str) -> list:
    """Splits a stored launcher command string into argv the way the
    running platform actually quotes paths (plan §3.2).

    POSIX: shlex.split(text, posix=True) — normal shell quoting.
    Windows: shlex.split(text, posix=True) mangles a bare backslash in
    something like `C:\\Users\\x\\claude.cmd`, eating it instead of keeping
    it literal, so Windows uses posix=False and then strips one matched
    pair of surrounding quotes from each token by hand, since posix=False
    leaves them in place. Unbalanced quotes raise ValueError from shlex
    itself in both modes — validated_settings_changes lets that propagate
    so an unparseable command never reaches disk."""
    if os.name == "nt":
        return [_strip_matched_quotes(tok) for tok in shlex.split(text, posix=False)]
    return shlex.split(text, posix=True)


def _strip_matched_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def launch_argv(kind: str) -> list:
    """The argv to launch `kind` with: the user's configured command, split
    once, or the registry default when nothing (or only whitespace) is
    configured. argv[0] is the executable; the rest are extra args the
    caller decides whether to honor (see cli.code())."""
    registered = _LAUNCHER_KINDS_BY_NAME.get(kind)
    default_command = registered.default_command if registered else kind
    try:
        text = (load_pool().settings.launchers.get(kind) or "").strip()
    except Exception:
        text = ""
    return _split_command(text or default_command)


def resolve_port(explicit: Optional[int]) -> int:
    """Port precedence (plan §3.4): explicit --port flag > CLAUDE_UNLIMITED_PORT
    env > settings.port > DEFAULT_PORT. argparse's own `default=` can't tell
    "flag omitted" from "flag given but happened to equal the default", so
    every --port subparser passes `default=None` and every reader calls this
    instead of reading args.port directly.

    Every candidate is range-checked against MIN_PORT/MAX_PORT — the same
    pair POST /api/settings/port enforces — so a port can't sail through the
    CLI that the Dashboard would reject. An out-of-range explicit --port
    raises ValueError (the user typed it and deserves to be told); an
    out-of-range env var or a hand-edited out-of-range settings.port falls
    through to the next candidate, exactly as a non-numeric env var already
    did.

    Raises:
        ValueError: `explicit` is outside MIN_PORT..MAX_PORT."""
    if explicit is not None:
        if not port_in_range(explicit):
            # An explicit --port is the one input a human definitely typed on
            # purpose, so an out-of-range one is an error, not something to
            # quietly paper over: silently falling back would start the daemon
            # on a port they did not ask for and never tell them why.
            raise ValueError(
                f"--port must be between {MIN_PORT} and {MAX_PORT} (got {explicit}); "
                f"anything below {MIN_PORT} needs privileges this daemon does not run with."
            )
        return explicit
    env = os.environ.get("CLAUDE_UNLIMITED_PORT")
    if env:
        try:
            candidate = int(env)
        except ValueError:
            candidate = None  # not a number; fall through to settings/default rather than crash
        # Out of range is treated the same way as not-a-number. The
        # environment is not a place a user watches for error messages — it
        # gets inherited from a service unit, a shell profile, a parent
        # process — so a bad value there falls back rather than refusing to
        # start something that was working yesterday.
        if candidate is not None and port_in_range(candidate):
            return candidate
    try:
        saved = load_pool().settings.port
    except Exception:
        return _default_port()
    # settings.port normally cannot be out of range — POST /api/settings/port
    # is the only writer and it validates — but config.json is a plain file a
    # user is invited to hand-edit (run_foreground's bind-failure message says
    # so in as many words), so this is not a can't-happen. Same treatment as a
    # bad env var: fall back rather than refuse to start.
    return saved if port_in_range(saved) else _default_port()


@dataclass
class Profile:
    """A single routable credential entry in the Pool.

    kind is one of:
      "oauth"  a Claude Pro/Max subscription;
      "api"    any Anthropic-compatible endpoint (base_url defaults to
               Anthropic's own API and can be pointed at a gateway);
      "codex"  a ChatGPT/Codex subscription, translated to and from
               Anthropic's Messages API shape.
    """

    id: str
    name: str
    kind: str = "oauth"  # oauth | api | codex
    base_url: Optional[str] = None  # api kind only; None/"" means api.anthropic.com
    auth_mode: str = "api_key"  # api_key | bearer; api kind only
    priority: int = 1
    switch_threshold: float = DEFAULT_SWITCH_THRESHOLD
    enabled: bool = True
    automatic: bool = False  # eligible for automatic Rotation, not just manual pin
    default_model: Optional[str] = None  # api kind only, optional
    monthly_budget_cap: Optional[float] = None  # api kind only, optional
    token_threshold: Optional[int] = None  # api kind only, optional: lifetime cumulative tokens at which Rotation stops picking this Profile. The api-kind analogue of switch_threshold, since an API key has no session-percentage window to measure against.
    tag_color: Optional[str] = None  # cosmetic only
    account_uuid: Optional[str] = None  # oauth kind only: the account identity Anthropic expects in the request body
    org_uuid: Optional[str] = None  # oauth kind only: organization.uuid from anthropic_oauth.fetch_account_profile; part of the dedup key alongside account_uuid (see profiles.find_by_account_and_org)
    organization_type: Optional[str] = None  # oauth kind only: organization.organization_type from anthropic_oauth.fetch_account_profile, e.g. "claude_max" | "claude_team"; drives anthropic_oauth.plan_from_account()
    plan: Optional[str] = None  # oauth kind only: "team" | "max" | "pro" | None (not yet detected), from anthropic_oauth.fetch_account_profile
    credential_updated_at: Optional[str] = None  # ISO timestamp bumped by profiles.update_credential(), so the live Gateway can notice a re-auth and clear a stuck AUTH_INVALID state
    claude_config_dir: Optional[str] = None  # oauth kind only: an isolated CLAUDE_CONFIG_DIR this Profile was authenticated under via `claude-unlimited add-account`, so it can be re-authenticated without touching another account's session. None for a Profile added by paste or "Import current login".
    codex_home: Optional[str] = None  # codex kind only: an isolated CODEX_HOME holding this Profile's auth.json, the counterpart of claude_config_dir. Every Codex invocation is scoped to it, so it never touches another Codex login on this machine.
    codex_model: Optional[str] = None  # codex kind only: overrides openai_models.py's mapping; None uses the automatic Claude-model -> Codex-model mapping.
    codex_reasoning_effort: Optional[str] = None  # codex kind only: overrides the reasoning-effort tier the mapping would pick (low|medium|high|xhigh|max|ultra); None uses the mapping's per-model default.
    codex_user_id: Optional[str] = None  # codex kind only: the id_token's chatgpt_user_id claim, part of the dedup key alongside account_uuid (see profiles.find_codex_profile). account_uuid alone is not unique for a codex Profile — two different ChatGPT users can share the same chatgpt_account_id (measured 2026-09-22). None on a Profile added before this field existed; profiles.find_codex_profile() resolves and backfills it locally from the Profile's own stored credential rather than trusting a later login's claim.


UPDATE_MODES = ("auto_install", "auto_download", "manual")


@dataclass
class Settings:
    """Everything that isn't a Profile: update behavior and notification
    preferences. Only what a user explicitly configured; runtime state
    (daemon uptime, activity) lives elsewhere."""

    update_mode: str = "auto_download"  # auto_install | auto_download | manual
    language: str = "en"  # ISO code matching a claude_unlimited/locales/<code>.json file
    notifications_enabled: bool = True
    notify_update_available: bool = True
    notify_approaching_threshold: bool = True
    notify_rotated: bool = False
    notify_quota_reset: bool = False
    notify_needs_attention: bool = True
    # The editable model-parity list: an ORDERED list of rows
    # [{"claude_model", "model"?, "effort"?, "claude_effort"?}, ...] that IS the
    # set of models Claude Code's /model picker offers for Codex-served
    # sessions. Empty ([] or {}) means "the default rows" (the 4 family heads),
    # never "advertise nothing". A legacy sparse dict {claude_id: {...}} is
    # still accepted and migrated at read time by openai_models.normalize_parity
    # — the config file keeps its old shape until the user's next save writes
    # the list, so no on-load rewrite is needed.
    model_parity: object = field(default_factory=dict)
    # {kind: "command string"} for kinds in LAUNCHER_KINDS, e.g.
    # {"claude": "claude --dangerously-skip-permissions"}. A kind missing here,
    # or mapped to "" / whitespace, falls back to that kind's default_command —
    # see launch_argv(). Free-form per PATCH /api/settings (validated by
    # _validated_launchers); NOT the port (see port below).
    launchers: dict = field(default_factory=dict)
    # The daemon's listen port. Readable via GET /api/settings but NOT
    # writable via PATCH /api/settings — changing it restarts the process, so
    # it goes through the dedicated POST /api/settings/port instead (plan
    # §3.5). validated_settings_changes rejects it explicitly rather than
    # letting the generic "unknown field" message send someone to the wrong
    # endpoint.
    port: int = field(default_factory=_default_port)


@dataclass
class Pool:
    profiles: List[Profile] = field(default_factory=list)
    shared_claude_dir: str = str(Path.home() / ".claude")
    settings: Settings = field(default_factory=Settings)

    def get(self, profile_id: str) -> Optional[Profile]:
        return next((p for p in self.profiles if p.id == profile_id), None)

    def enabled_profiles(self) -> List[Profile]:
        return [p for p in self.profiles if p.enabled]


def ensure_app_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(APP_DIR, 0o700)
    except OSError:
        pass


def load_pool() -> Pool:
    ensure_app_dir()
    if not CONFIG_FILE.exists():
        return Pool(profiles=[])
    data = json.loads(CONFIG_FILE.read_text())
    profiles = [
        Profile(
            id=p["id"],
            name=p["name"],
            kind=p.get("kind", "oauth"),
            base_url=p.get("base_url"),
            auth_mode=p.get("auth_mode", "api_key"),
            priority=int(p.get("priority", 1)),
            switch_threshold=float(p.get("switch_threshold", DEFAULT_SWITCH_THRESHOLD)),
            enabled=bool(p.get("enabled", True)),
            automatic=bool(p.get("automatic", False)),
            default_model=p.get("default_model"),
            monthly_budget_cap=p.get("monthly_budget_cap"),
            token_threshold=p.get("token_threshold"),
            tag_color=p.get("tag_color"),
            account_uuid=p.get("account_uuid"),
            org_uuid=p.get("org_uuid"),
            organization_type=p.get("organization_type"),
            plan=p.get("plan"),
            credential_updated_at=p.get("credential_updated_at"),
            claude_config_dir=p.get("claude_config_dir"),
            codex_home=p.get("codex_home"),
            codex_model=p.get("codex_model"),
            codex_reasoning_effort=p.get("codex_reasoning_effort"),
            codex_user_id=p.get("codex_user_id"),
        )
        for p in data.get("profiles", [])
    ]
    settings_data = data.get("settings", {})
    settings = Settings(
        update_mode=settings_data.get("update_mode", "auto_download"),
        language=settings_data.get("language", "en"),
        notifications_enabled=bool(settings_data.get("notifications_enabled", True)),
        notify_update_available=bool(settings_data.get("notify_update_available", True)),
        notify_approaching_threshold=bool(settings_data.get("notify_approaching_threshold", True)),
        notify_rotated=bool(settings_data.get("notify_rotated", False)),
        notify_quota_reset=bool(settings_data.get("notify_quota_reset", False)),
        notify_needs_attention=bool(settings_data.get("notify_needs_attention", True)),
        model_parity=settings_data.get("model_parity") or {},
        launchers=dict(settings_data.get("launchers") or {}),
        port=int(settings_data["port"]) if settings_data.get("port") is not None else _default_port(),
    )

    return Pool(
        profiles=profiles,
        shared_claude_dir=data.get("shared_claude_dir", str(Path.home() / ".claude")),
        settings=settings,
    )


def save_pool(pool: Pool) -> None:
    ensure_app_dir()
    payload = {
        "profiles": [asdict(p) for p in pool.profiles],
        "shared_claude_dir": pool.shared_claude_dir,
        "settings": asdict(pool.settings),
    }
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(CONFIG_FILE)


_SETTINGS_FIELDS = {
    "update_mode", "language", "notifications_enabled", "notify_update_available",
    "notify_approaching_threshold", "notify_rotated", "notify_quota_reset", "notify_needs_attention",
    "model_parity", "launchers",
    # "port" is deliberately absent: it is readable but not PATCHable (see
    # validated_settings_changes and Settings.port above).
}


def _validated_launchers(raw) -> dict:
    """Validates a `settings.launchers` payload: every key must be a
    LAUNCHER_KINDS entry and every value must be a string that parses under
    _split_command, so a bad quote can never reach disk. An empty string is
    valid — see launch_argv()'s fallback to the registry default."""
    if not isinstance(raw, dict):
        raise ValueError("launchers must be an object of {kind: command string}")
    cleaned: dict = {}
    for kind, command in raw.items():
        if kind not in _LAUNCHER_KINDS_BY_NAME:
            raise ValueError(f"launchers has an unknown CLI kind: {kind!r}")
        if not isinstance(command, str):
            raise ValueError(f"launchers[{kind!r}] must be a string")
        _split_command(command)  # raises ValueError on unbalanced quotes
        cleaned[kind] = command
    return cleaned


def _validated_model_row_fields(where, row, entry):
    """Validate the model/effort/claude_effort of one parity row into `entry`.
    Shared by the dict (legacy) and list (current) shapes."""
    from .openai_models import VALID_REASONING_EFFORTS, CLAUDE_REASONING_EFFORTS

    model = row.get("model")
    if model is not None:
        # Left free-form on purpose: a Profile override already accepts an
        # arbitrary model id, and pinning one this build has not heard of is
        # legitimate. The fallback ladder handles a rejected model.
        if not isinstance(model, str) or not model.strip() or len(model) > 128:
            raise ValueError(f"{where}.model must be a short non-empty string")
        entry["model"] = model.strip()
    effort = row.get("effort")
    if effort is not None:
        if effort not in VALID_REASONING_EFFORTS:
            raise ValueError(f"{where}.effort must be one of {list(VALID_REASONING_EFFORTS)}")
        entry["effort"] = effort
    claude_effort = row.get("claude_effort")
    if claude_effort is not None:
        if claude_effort not in CLAUDE_REASONING_EFFORTS:
            raise ValueError(
                f"{where}.claude_effort must be one of {list(CLAUDE_REASONING_EFFORTS)}")
        entry["claude_effort"] = claude_effort
    return entry


def _validated_model_parity(raw):
    """Validates a parity payload, rejecting the whole thing rather than
    silently dropping a bad row — a mapping that half-applied would be worse
    than one that refused.

    Accepts BOTH shapes so old config files and export bundles keep working:
      * the current ORDERED LIST of rows [{claude_model, model?, effort?,
        claude_effort?}, ...] — the editable parity list; order is preserved;
      * the legacy sparse dict {claude_id: {model?, effort?, claude_effort?}}.
    openai_models.normalize_parity() turns either into the in-force row list at
    read time, and the empty case ([]/{}) means "the default rows"."""
    from .model_catalogue import base_id

    if isinstance(raw, list):
        if len(raw) > 64:
            raise ValueError("model_parity has too many entries")
        cleaned_list: list = []
        seen_bases: set = set()
        for i, row in enumerate(raw):
            if not isinstance(row, dict):
                raise ValueError(f"model_parity[{i}] must be an object")
            claude_id = row.get("claude_model")
            if not isinstance(claude_id, str) or not claude_id.strip() or len(claude_id) > 128:
                raise ValueError(f"model_parity[{i}].claude_model must be a short non-empty model id")
            claude_id = claude_id.strip()
            base = base_id(claude_id)
            if base in seen_bases:
                raise ValueError(f"model_parity has a duplicate Claude model: {claude_id}")
            seen_bases.add(base)
            entry = {"claude_model": claude_id}
            _validated_model_row_fields(f"model_parity[{i}]", row, entry)
            cleaned_list.append(entry)
        return cleaned_list

    if not isinstance(raw, dict):
        raise ValueError("model_parity must be a list of rows or an object")
    if len(raw) > 64:
        raise ValueError("model_parity has too many entries")
    cleaned: dict = {}
    for claude_id, row in raw.items():
        if not isinstance(claude_id, str) or not claude_id.strip():
            raise ValueError("model_parity keys must be non-empty model ids")
        if not isinstance(row, dict):
            raise ValueError(f"model_parity[{claude_id}] must be an object")
        entry = _validated_model_row_fields(f"model_parity[{claude_id}]", row, {})
        if entry:
            cleaned[claude_id.strip()] = entry
    return cleaned


def validated_settings_changes(changes: dict) -> dict:
    """Validates a settings payload. Shared by PATCH /api/settings and by
    bundle import, so an imported bundle cannot set something the API would
    have refused."""
    if "port" in changes:
        # Named explicitly, ahead of the generic unknown-field check below,
        # so the message points at the right endpoint instead of just
        # saying "port" is unrecognized (plan §3.5: changing the port
        # restarts the daemon, so it isn't a plain field write).
        raise ValueError(
            "port cannot be changed via PATCH /api/settings — it restarts the daemon, so use "
            "POST /api/settings/port instead."
        )
    unknown = set(changes) - _SETTINGS_FIELDS
    if unknown:
        raise ValueError(f"Cannot change settings fields: {sorted(unknown)}")
    changes = dict(changes)
    if "update_mode" in changes and changes["update_mode"] not in UPDATE_MODES:
        raise ValueError(f"update_mode must be one of {UPDATE_MODES}")
    if "model_parity" in changes:
        changes["model_parity"] = _validated_model_parity(changes["model_parity"])
    if "launchers" in changes:
        changes["launchers"] = _validated_launchers(changes["launchers"])
    if "language" in changes:
        from . import i18n

        if changes["language"] not in i18n.list_locales():
            raise ValueError(f"language must be one of {i18n.list_locales()}")
    return changes


def update_settings(**changes) -> Settings:
    changes = validated_settings_changes(changes)
    with CONFIG_LOCK:
        pool = load_pool()
        pool.settings = replace(pool.settings, **changes)
        save_pool(pool)
        return pool.settings


def set_port(port: int) -> Settings:
    """The one write path for settings.port. update_settings() can't be it —
    validated_settings_changes rejects "port" outright, on purpose, so a
    PATCH or a bundle import can never move it (see Settings.port and
    validated_settings_changes above). POST /api/settings/port is the only
    caller, and only after its own range check and bind preflight, so no
    validation is duplicated here."""
    with CONFIG_LOCK:
        pool = load_pool()
        pool.settings = replace(pool.settings, port=port)
        save_pool(pool)
        return pool.settings
