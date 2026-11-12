import json
from datetime import datetime, timedelta, timezone

import pytest

import claude_unlimited.session_tokens as session_tokens


class FrozenClock:
    def __init__(self):
        self.current = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.current

    def advance(self, **kwargs):
        self.current += timedelta(**kwargs)


@pytest.fixture(autouse=True)
def isolated_file(tmp_path, monkeypatch):
    monkeypatch.setattr(session_tokens, "SESSION_TOKENS_FILE", tmp_path / "session_tokens.json")


@pytest.fixture
def frozen_clock(monkeypatch):
    clock = FrozenClock()
    monkeypatch.setattr(session_tokens, "_now", clock)
    return clock


def test_resolve_unknown_token_returns_none():
    assert session_tokens.resolve("nope") is None


def test_get_or_create_then_resolve_round_trips_the_profile_id():
    token = session_tokens.get_or_create("prof-a")
    assert session_tokens.resolve(token) == "prof-a"


def test_get_or_create_reuses_the_existing_token_for_the_same_profile():
    first = session_tokens.get_or_create("prof-a")
    second = session_tokens.get_or_create("prof-a")
    assert first == second


def test_get_or_create_mints_distinct_tokens_for_distinct_profiles():
    a = session_tokens.get_or_create("prof-a")
    b = session_tokens.get_or_create("prof-b")
    assert a != b
    assert session_tokens.resolve(a) == "prof-a"
    assert session_tokens.resolve(b) == "prof-b"


def test_expired_token_no_longer_resolves():
    token = session_tokens.get_or_create("prof-a")
    data = session_tokens._load()
    stale = (datetime.now(timezone.utc) - session_tokens.SESSION_TOKEN_TTL - timedelta(days=1)).isoformat()
    data[token]["created_at"] = stale
    session_tokens._save(data)
    assert session_tokens.resolve(token) is None


def test_token_created_31_days_ago_but_used_1_day_ago_still_resolves(frozen_clock):
    token = session_tokens.get_or_create("prof-a")
    data = session_tokens._load()
    data[token]["created_at"] = (frozen_clock.current - timedelta(days=31)).isoformat()
    data[token]["last_used_at"] = (frozen_clock.current - timedelta(days=1)).isoformat()
    session_tokens._save(data)

    assert session_tokens.resolve(token) == "prof-a"


def test_token_idle_31_days_does_not_resolve(frozen_clock):
    token = session_tokens.get_or_create("prof-a")
    data = session_tokens._load()
    stale = frozen_clock.current - session_tokens.SESSION_TOKEN_TTL - timedelta(days=1)
    data[token]["last_used_at"] = stale.isoformat()
    session_tokens._save(data)

    assert session_tokens.resolve_grant(token) == session_tokens.SessionGrant()


def test_legacy_entry_created_31_days_ago_does_not_resolve(frozen_clock):
    token = session_tokens.get_or_create("prof-a")
    data = session_tokens._load()
    stale = frozen_clock.current - session_tokens.SESSION_TOKEN_TTL - timedelta(days=1)
    data[token]["created_at"] = stale.isoformat()
    data[token].pop("last_used_at", None)
    session_tokens._save(data)

    assert session_tokens.resolve_grant(token) == session_tokens.SessionGrant()


def test_legacy_entry_created_29_days_ago_resolves(frozen_clock):
    token = session_tokens.get_or_create("prof-a")
    data = session_tokens._load()
    data[token]["created_at"] = (frozen_clock.current - timedelta(days=29)).isoformat()
    data[token].pop("last_used_at", None)
    session_tokens._save(data)

    assert session_tokens.resolve(token) == "prof-a"


def test_reusing_token_records_last_used_at(frozen_clock):
    token = session_tokens.get_or_create("prof-a")
    frozen_clock.advance(minutes=5)

    assert session_tokens.get_or_create("prof-a") == token
    data = session_tokens._load()
    assert datetime.fromisoformat(data[token]["last_used_at"]) == frozen_clock.current


def test_resolve_grant_persists_last_use_at_most_once_per_hour(frozen_clock, monkeypatch):
    token = session_tokens.get_or_create("prof-a")
    real_save = session_tokens._save
    saves = []

    def counting_save(data):
        saves.append(data[token].get("last_used_at"))
        real_save(data)

    monkeypatch.setattr(session_tokens, "_save", counting_save)
    for _ in range(100):
        assert session_tokens.resolve_grant(token) == session_tokens.SessionGrant(forced_profile_id="prof-a")

    assert len(saves) <= 1


def test_reused_distribute_token_used_across_45_days_never_stops_resolving(frozen_clock):
    token = session_tokens.get_or_create_distribute()

    for day in range(46):
        frozen_clock.current = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day)
        assert session_tokens.get_or_create_distribute() == token
        assert session_tokens.resolve_grant(token) == session_tokens.SessionGrant(distribute=True)


def test_get_or_create_does_not_prune_a_token_that_resolve_grant_accepts(frozen_clock):
    token = session_tokens.get_or_create("prof-a")
    data = session_tokens._load()
    data[token]["created_at"] = (frozen_clock.current - timedelta(days=31)).isoformat()
    data[token]["last_used_at"] = (frozen_clock.current - timedelta(days=1)).isoformat()
    session_tokens._save(data)

    assert session_tokens.get_or_create("prof-a") == token
    assert session_tokens.resolve(token) == "prof-a"


def test_get_or_create_after_expiry_mints_a_fresh_token_not_the_stale_one():
    token = session_tokens.get_or_create("prof-a")
    data = session_tokens._load()
    stale = (datetime.now(timezone.utc) - session_tokens.SESSION_TOKEN_TTL - timedelta(days=1)).isoformat()
    data[token]["created_at"] = stale
    session_tokens._save(data)

    fresh = session_tokens.get_or_create("prof-a")
    assert fresh != token
    assert session_tokens.resolve(fresh) == "prof-a"


def test_resolve_grant_of_unknown_token_is_empty():
    grant = session_tokens.resolve_grant("nope")
    assert grant.forced_profile_id is None
    assert grant.distribute is False


def test_distribute_token_grants_distribute_and_no_pin():
    token = session_tokens.get_or_create_distribute()
    grant = session_tokens.resolve_grant(token)
    assert grant.distribute is True
    assert grant.forced_profile_id is None
    # The pin-only view of the same token must not invent a profile.
    assert session_tokens.resolve(token) is None


def test_get_or_create_distribute_reuses_its_token():
    assert session_tokens.get_or_create_distribute() == session_tokens.get_or_create_distribute()


def test_distribute_and_pinned_tokens_are_distinct():
    pinned = session_tokens.get_or_create("prof-a")
    distributed = session_tokens.get_or_create_distribute()
    assert pinned != distributed
    assert session_tokens.resolve_grant(pinned) == session_tokens.SessionGrant(forced_profile_id="prof-a")
    assert session_tokens.resolve_grant(distributed) == session_tokens.SessionGrant(distribute=True)


def test_get_or_create_distribute_does_not_hijack_a_pinned_entry():
    session_tokens.get_or_create("prof-a")
    token = session_tokens.get_or_create_distribute()
    assert session_tokens._load()[token].get("profile_id") is None


def test_a_pin_wins_over_distribute_if_an_entry_ever_carries_both():
    token = session_tokens.get_or_create("prof-a")
    data = session_tokens._load()
    data[token]["mode"] = "distribute"
    session_tokens._save(data)

    grant = session_tokens.resolve_grant(token)
    assert grant.forced_profile_id == "prof-a"
    assert grant.distribute is False


def test_expired_distribute_token_no_longer_grants_anything():
    token = session_tokens.get_or_create_distribute()
    data = session_tokens._load()
    stale = (datetime.now(timezone.utc) - session_tokens.SESSION_TOKEN_TTL - timedelta(days=1)).isoformat()
    data[token]["created_at"] = stale
    session_tokens._save(data)
    assert session_tokens.resolve_grant(token) == session_tokens.SessionGrant()


@pytest.fixture
def token_store(tmp_path, monkeypatch):
    token_file = tmp_path / "session_tokens.json"
    now = datetime(2026, 11, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(session_tokens, "SESSION_TOKENS_FILE", token_file)
    monkeypatch.setattr(session_tokens, "_now", lambda: now)
    return token_file, now


def test_token_status_uses_sliding_expiry_and_does_not_write(token_store, monkeypatch):
    token_file, now = token_store
    live_token = "fixture-live-token"
    expired_token = "fixture-expired-token"
    token_file.write_text(json.dumps({
        live_token: {
            "mode": "distribute",
            "created_at": (now - timedelta(days=31)).isoformat(),
            "last_used_at": (now - timedelta(days=1)).isoformat(),
        },
        expired_token: {
            "profile_id": "profile-a",
            "created_at": (now - timedelta(days=31)).isoformat(),
            "last_used_at": (now - timedelta(days=31)).isoformat(),
        },
    }))
    before = token_file.read_bytes()
    monkeypatch.setattr(session_tokens, "_save", lambda _data: pytest.fail("token_status must not write"))

    assert session_tokens.token_status(live_token) == "live"
    assert session_tokens.token_status(expired_token) == "expired"
    assert session_tokens.token_status("fixture-unknown-token") == "unknown"
    assert token_file.read_bytes() == before


def test_token_details_exposes_only_kind_and_effective_last_use(token_store):
    token_file, now = token_store
    token = "fixture-profile-token"
    last_used_at = (now - timedelta(days=31)).isoformat()
    token_file.write_text(json.dumps({token: {
        "profile_id": "profile-a",
        "created_at": (now - timedelta(days=32)).isoformat(),
        "last_used_at": last_used_at,
    }}))

    details = session_tokens.token_details(token)

    assert details is not None
    assert details.kind == "profile:profile-a"
    assert details.last_used_at == last_used_at
    assert token not in repr(details)
