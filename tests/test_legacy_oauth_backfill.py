"""Regression coverage for the daemon-side legacy-organization backfill:
daemon._backfill_legacy_oauth_organizations() fills org_uuid/organization_type
/plan onto oauth Profiles that predate organization identity, from each
Profile's OWN stored credential — no login, no user involvement.

No test in this file makes a real network call: every
anthropic_oauth.fetch_account_profile() is monkeypatched.
"""

import threading

import pytest

import claude_unlimited.activity as activity
import claude_unlimited.anthropic_oauth as anthropic_oauth
import claude_unlimited.daemon as daemon
import claude_unlimited.profiles as profile_repo


class FakeSecretStore:
    def __init__(self):
        self.tokens = {}

    def set_token(self, profile_id, token):
        self.tokens[profile_id] = token

    def get_token(self, profile_id):
        return self.tokens[profile_id]

    def delete_token(self, profile_id):
        self.tokens.pop(profile_id, None)

    def has_token(self, profile_id):
        return profile_id in self.tokens


@pytest.fixture
def env(monkeypatch, tmp_path):
    store = FakeSecretStore()
    monkeypatch.setattr(profile_repo, "secret_store", store)
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    return store


def _account(**overrides):
    base = dict(
        account_uuid="uuid-shared", email="dev@example.com", display_name="Dev",
        org_uuid="org-x", org_name="Some Org", organization_type="claude_max",
        has_claude_max=True, has_claude_pro=False,
    )
    base.update(overrides)
    return anthropic_oauth.AccountProfile(**base)


def _fetch_by_token(mapping):
    def fake(access_token, timeout=15.0):
        try:
            return mapping[access_token]
        except KeyError:
            raise anthropic_oauth.ProfileLookupError(f"no fixture for token {access_token!r}")
    return fake


# ---------------------------------------------------------------------------
# The happy path.
# ---------------------------------------------------------------------------

def test_backfills_org_fields_and_plan_from_own_credential(env, monkeypatch):
    legacy = profile_repo.create_profile(
        name="Legacy", kind="oauth", credential="team-token-long", account_uuid="uuid-shared")
    assert legacy.org_uuid is None

    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", _fetch_by_token({
        "team-token-long": _account(
            account_uuid="uuid-shared", org_uuid="org-team", org_name="Acme Team",
            organization_type="claude_team", has_claude_max=True, has_claude_pro=False),
    }))

    daemon._backfill_legacy_oauth_organizations()

    reloaded = profile_repo.find_by_account_uuid("uuid-shared")
    assert reloaded.org_uuid == "org-team"
    assert reloaded.organization_type == "claude_team"
    assert reloaded.plan == "team"
    # Name and credential are untouched.
    assert reloaded.name == "Legacy"
    assert env.get_token(legacy.id) == "team-token-long"


def test_activity_records_profile_and_org_never_the_token(env, monkeypatch):
    profile_repo.create_profile(
        name="Legacy", kind="oauth", credential="super-secret-token-long", account_uuid="uuid-shared")
    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", _fetch_by_token({
        "super-secret-token-long": _account(
            account_uuid="uuid-shared", org_uuid="org-team", organization_type="claude_team"),
    }))

    daemon._backfill_legacy_oauth_organizations()

    events = activity.list_events()
    assert any("Legacy" in e.text for e in events)
    assert any(e.meta and "org-team" in e.meta for e in events)
    dump = "\n".join(f"{e.text} {e.meta or ''}" for e in events)
    assert "super-secret-token-long" not in dump


# ---------------------------------------------------------------------------
# The mismatch guard: never write on a mismatched account_uuid.
# ---------------------------------------------------------------------------

def test_mismatched_account_uuid_is_skipped_and_writes_nothing(env, monkeypatch):
    legacy = profile_repo.create_profile(
        name="Legacy", kind="oauth", credential="swapped-token-long", account_uuid="uuid-recorded")
    original_stored = env.get_token(legacy.id)

    # The credential's own resolution reports a DIFFERENT account_uuid than
    # what's stored on the Profile — a replaced-out-from-under-it credential.
    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", _fetch_by_token({
        "swapped-token-long": _account(
            account_uuid="uuid-different", org_uuid="org-x", organization_type="claude_max"),
    }))

    daemon._backfill_legacy_oauth_organizations()

    reloaded = profile_repo.find_by_account_uuid("uuid-recorded")
    assert reloaded.org_uuid is None
    assert reloaded.organization_type is None
    assert reloaded.plan is None
    assert env.get_token(legacy.id) == original_stored


# ---------------------------------------------------------------------------
# Idempotence and kind-filtering: no network call at all when skipped.
# ---------------------------------------------------------------------------

def test_profile_with_org_uuid_already_set_is_skipped_with_no_network_call(env, monkeypatch):
    profile_repo.create_profile(
        name="Already Identified", kind="oauth", credential="tok-already-long",
        account_uuid="uuid-1", org_uuid="org-existing", organization_type="claude_max")

    called = []

    def fake(access_token, timeout=15.0):
        called.append(access_token)
        return _account()

    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", fake)

    daemon._backfill_legacy_oauth_organizations()

    assert called == []
    reloaded = profile_repo.find_by_account_uuid("uuid-1")
    assert reloaded.org_uuid == "org-existing"  # unchanged


def test_non_oauth_kinds_are_skipped_with_no_network_call(env, monkeypatch):
    profile_repo.create_profile(name="API Key", kind="api", credential="api-key-long-enough")
    profile_repo.create_profile(
        name="Codex", kind="codex", credential="codex-token-long", account_uuid="uuid-codex")

    called = []

    def fake(access_token, timeout=15.0):
        called.append(access_token)
        return _account()

    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", fake)

    daemon._backfill_legacy_oauth_organizations()

    assert called == []


# ---------------------------------------------------------------------------
# Best-effort discipline: never raises, one bad Profile never blocks another.
# ---------------------------------------------------------------------------

def test_unresolvable_profile_is_left_alone_no_crash(env, monkeypatch):
    profile_repo.create_profile(
        name="Dead Token", kind="oauth", credential="dead-token-long", account_uuid="uuid-1")

    def boom(access_token, timeout=15.0):
        raise anthropic_oauth.ProfileLookupError("token rejected")

    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", boom)

    daemon._backfill_legacy_oauth_organizations()  # must not raise

    reloaded = profile_repo.find_by_account_uuid("uuid-1")
    assert reloaded.org_uuid is None


def test_one_unresolvable_profile_does_not_block_a_later_one(env, monkeypatch):
    dead = profile_repo.create_profile(
        name="Dead", kind="oauth", credential="dead-token-long", account_uuid="uuid-dead")
    alive = profile_repo.create_profile(
        name="Alive", kind="oauth", credential="alive-token-long", account_uuid="uuid-alive")

    def fake(access_token, timeout=15.0):
        if access_token == "dead-token-long":
            raise anthropic_oauth.ProfileLookupError("expired")
        return _account(account_uuid="uuid-alive", org_uuid="org-alive", organization_type="claude_max")

    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", fake)

    daemon._backfill_legacy_oauth_organizations()

    assert profile_repo.find_by_account_uuid("uuid-dead").org_uuid is None
    assert profile_repo.find_by_account_uuid("uuid-alive").org_uuid == "org-alive"


def test_list_profiles_failure_never_raises(env, monkeypatch):
    def boom():
        raise RuntimeError("config.json unreadable")

    monkeypatch.setattr(profile_repo, "list_profiles", boom)
    daemon._backfill_legacy_oauth_organizations()  # must not raise


def test_update_profile_failure_for_one_profile_never_raises_and_does_not_block_others(env, monkeypatch):
    profile_repo.create_profile(
        name="First", kind="oauth", credential="first-token-long", account_uuid="uuid-first")
    profile_repo.create_profile(
        name="Second", kind="oauth", credential="second-token-long", account_uuid="uuid-second")

    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", _fetch_by_token({
        "first-token-long": _account(account_uuid="uuid-first", org_uuid="org-first", organization_type="claude_max"),
        "second-token-long": _account(account_uuid="uuid-second", org_uuid="org-second", organization_type="claude_max"),
    }))

    real_update = profile_repo.update_profile
    calls = []

    def flaky_update(profile_id, **changes):
        calls.append(profile_id)
        if len(calls) == 1:
            raise RuntimeError("simulated write failure")
        return real_update(profile_id, **changes)

    monkeypatch.setattr(daemon.profile_repo, "update_profile", flaky_update)

    daemon._backfill_legacy_oauth_organizations()  # must not raise

    # The second Profile still gets backfilled despite the first failing.
    assert profile_repo.find_by_account_uuid("uuid-second").org_uuid == "org-second"


# ---------------------------------------------------------------------------
# The startup async wrapper.
# ---------------------------------------------------------------------------

def test_async_wrapper_runs_pass_in_a_background_daemon_thread(env, monkeypatch):
    profile_repo.create_profile(
        name="Legacy", kind="oauth", credential="team-token-long", account_uuid="uuid-shared")
    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", _fetch_by_token({
        "team-token-long": _account(
            account_uuid="uuid-shared", org_uuid="org-team", organization_type="claude_team"),
    }))

    before = {t.ident for t in threading.enumerate()}
    daemon._backfill_legacy_oauth_organizations_async()
    new_threads = [t for t in threading.enumerate() if t.ident not in before]
    assert len(new_threads) == 1
    assert new_threads[0].daemon is True
    new_threads[0].join(timeout=2)
    assert not new_threads[0].is_alive()

    reloaded = profile_repo.find_by_account_uuid("uuid-shared")
    assert reloaded.org_uuid == "org-team"
