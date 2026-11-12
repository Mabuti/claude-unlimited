"""Persists the Dashboard-visible slice of Gateway's live runtime state
(usage %, reset times, whether a Profile has no 5h window, and which Profile
is current) across daemon restarts.
Valid, unexpired usage numbers are restored and may drive Rotation; quota
state is mostly re-derived rather than persisted, with AUTH_INVALID and a
future COOLDOWN restored when their persisted state is still valid.

EXHAUSTED and DRAINING state are deliberately NOT persisted; ELIGIBLE versus
DRAINING is re-derived from any valid usage number restored alongside them.
AUTH_INVALID is preserved because a restart cannot repair a rejected
credential. COOLDOWN is preserved only while its cooldown_until deadline is
still in the future. The last-observed usage numbers and the 5h-window
capability flag are restored, so the Dashboard doesn't show a wall of "not
yet observed" after every restart. A number whose resets_at has already
passed at load time is dropped rather than restored: a stale percentage past
its own reset is actively wrong, and the next request produces a fresh one
anyway.
"""

from __future__ import annotations

import json
import threading
from typing import Optional

from .config import APP_DIR, ensure_app_dir

RUNTIME_STATE_FILE = APP_DIR / "runtime_state.json"
_lock = threading.Lock()


def load() -> dict:
    """{"current_profile_id": str|None, "profiles": {profile_id: {...}}}.

    Never raises: a missing or corrupt file means nothing to restore,
    exactly like a first run."""
    empty = {"current_profile_id": None, "profiles": {}}
    if not RUNTIME_STATE_FILE.exists():
        return empty
    try:
        data = json.loads(RUNTIME_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(data, dict):
        return empty
    profiles = data.get("profiles")
    return {
        "current_profile_id": data.get("current_profile_id"),
        "profiles": profiles if isinstance(profiles, dict) else {},
    }


def save(current_profile_id: Optional[str], profiles: dict) -> None:
    """Best-effort: called after every observation, so it must never raise
    into the request path. Callers wrap this."""
    ensure_app_dir()
    payload = {"current_profile_id": current_profile_id, "profiles": profiles}
    tmp = RUNTIME_STATE_FILE.with_suffix(".json.tmp")
    with _lock:
        tmp.write_text(json.dumps(payload))
        tmp.replace(RUNTIME_STATE_FILE)
