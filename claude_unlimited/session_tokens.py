"""Per-terminal-session credentials for `claude-unlimited code --profile`.

The shared placeholder_token only proves the request came from a process on
this machine, and every invocation uses the same one, so it cannot carry any
extra meaning. Pinning one terminal to one specific Profile, without
affecting other concurrent terminals, needs a distinct credential per pinned
session; that is what this module mints.

get_or_create() reuses a live token for the same profile_id instead of
minting a fresh one per call, so repeated invocations don't grow the file
unboundedly. Tokens expire after SESSION_TOKEN_TTL of inactivity: accepted
requests and token reuse slide the expiry forward, while persisted last-use
timestamps are throttled so a long-running terminal session isn't invalidated
under itself and dead entries don't accumulate indefinitely.
"""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import APP_DIR, ensure_app_dir

SESSION_TOKENS_FILE = APP_DIR / "session_tokens.json"
SESSION_TOKEN_TTL = timedelta(days=30)
LAST_USED_WRITE_INTERVAL = timedelta(hours=1)
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load() -> dict:
    if not SESSION_TOKENS_FILE.exists():
        return {}
    try:
        data = json.loads(SESSION_TOKENS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    ensure_app_dir()
    tmp = SESSION_TOKENS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(SESSION_TOKENS_FILE)


def _parse_timestamp(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_expired(entry: dict, now: datetime) -> bool:
    created_at = _parse_timestamp(entry.get("created_at"))
    if created_at is None:
        return True
    last_used_at = entry.get("last_used_at")
    reference = _parse_timestamp(last_used_at) if last_used_at else created_at
    if reference is None:
        return True
    return now - reference > SESSION_TOKEN_TTL


def _record_use(entry: dict, now: datetime) -> bool:
    stored = _parse_timestamp(entry.get("last_used_at"))
    if stored is not None and now - stored < LAST_USED_WRITE_INTERVAL:
        return False
    entry["last_used_at"] = now.isoformat()
    return True


@dataclass(frozen=True)
class SessionGrant:
    """What a session token entitles this terminal to.

    Exactly one of the two is ever meaningful: `forced_profile_id` pins every
    request to one Profile (`--profile`), while `distribute` spreads a
    session's branches — its main agent and each subagent — across accounts
    (`--distribute`). They contradict each other, so the CLI refuses both and
    a forced pin wins if an old entry ever carried both."""

    forced_profile_id: Optional[str] = None
    distribute: bool = False


@dataclass(frozen=True)
class SessionTokenDetails:
    """Non-secret details used to explain why a session token was rejected."""

    kind: str
    last_used_at: Optional[str]


def get_or_create(profile_id: str) -> str:
    """Returns a token that resolve() will map back to `profile_id`. Reuses
    an existing live one for the same profile_id if there is one, instead
    of minting a new one on every call."""
    return _get_or_create_entry({"profile_id": profile_id},
                                lambda e: e.get("profile_id") == profile_id)


def get_or_create_distribute() -> str:
    """A token meaning "distribute this session's branches across accounts"
    rather than "pin to one Profile". Reused across launches like the pinned
    kind, so repeated `cu code --distribute` runs don't grow the file."""
    return _get_or_create_entry({"mode": "distribute"},
                                lambda e: e.get("mode") == "distribute" and not e.get("profile_id"))


def _get_or_create_entry(fields: dict, matches) -> str:
    with _lock:
        now = _now()
        data = _load()
        # Prune expired entries and look for a reusable one in the same pass.
        alive = {tok: entry for tok, entry in data.items() if not _is_expired(entry, now)}
        for tok, entry in alive.items():
            if matches(entry):
                touched = _record_use(entry, now)
                if touched or len(alive) != len(data):
                    _save(alive)
                return tok
        token = secrets.token_urlsafe(32)
        alive[token] = {**fields, "created_at": now.isoformat()}
        _save(alive)
        return token


def resolve(token: str) -> Optional[str]:
    """The profile_id this token is pinned to, or None. Kept for callers that
    only care about pinning; `resolve_grant` is the fuller answer."""
    return resolve_grant(token).forced_profile_id


def resolve_grant(token: str) -> SessionGrant:
    """What this token grants, or an empty grant if it is unknown or expired.
    Not a security check on its own — it is an equality comparison against a
    large random token, the same trust model as placeholder_token — and the
    caller decides what "not found" means."""
    with _lock:
        now = _now()
        data = _load()
        entry = data.get(token)
        if entry is None or _is_expired(entry, now):
            return SessionGrant()
        if _record_use(entry, now):
            _save(data)
        profile_id = entry.get("profile_id")
        if profile_id:
            return SessionGrant(forced_profile_id=profile_id)  # a pin always wins
        return SessionGrant(distribute=entry.get("mode") == "distribute")


def token_status(token: str) -> str:
    """Classify a token without changing the session-token file.

    The result is ``live`` for an entry that still passes the sliding expiry
    rule, ``expired`` for an entry that does not, and ``unknown`` when the
    token is not present.  The token itself is never returned or logged.
    """
    with _lock:
        entry = _load().get(token)
        if entry is None:
            return "unknown"
        return "live" if isinstance(entry, dict) and not _is_expired(entry, _now()) else "expired"


def token_details(token: str) -> Optional[SessionTokenDetails]:
    """Return safe explanatory details for a stored token entry.

    This deliberately omits the token value.  ``created_at`` is the effective
    last-use time for legacy entries that have no persisted ``last_used_at``.
    """
    with _lock:
        entry = _load().get(token)
        if not isinstance(entry, dict):
            return None
        profile_id = entry.get("profile_id")
        if profile_id:
            kind = f"profile:{profile_id}"
        elif entry.get("mode") == "distribute":
            kind = "distribute"
        else:
            kind = "unknown"
        last_used_at = entry.get("last_used_at") or entry.get("created_at")
        return SessionTokenDetails(kind=kind, last_used_at=last_used_at if isinstance(last_used_at, str) else None)
