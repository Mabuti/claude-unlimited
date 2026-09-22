"""Regression coverage for the account-identity data-loss fix: a login must
never be able to overwrite an existing Profile's credential with a
subscription from a DIFFERENT organization.

Background incident (2026-09-22): one Anthropic account_uuid carried two
subscriptions under two organizations — a Team seat and a personal Max plan.
A legacy (org_uuid=None) Profile held the Team credential. Running
`add-account` to refresh it logged into the PERSONAL organization instead;
the old legacy-match path trusted that NEXT login's org_uuid to decide what
the pre-existing Profile was, reused it, and silently overwrote the Team
credential. The fix: a legacy match is now resolved from the EXISTING
Profile's OWN stored credential (profiles.resolve_profile_own_identity())
before it is ever reused — see profiles.resolve_legacy_oauth_match().

No test in this file makes a real network call: every anthropic_oauth.
fetch_account_profile() is monkeypatched.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

import claude_unlimited.anthropic_oauth as anthropic_oauth
import claude_unlimited.cli as cli
import claude_unlimited.daemon as daemon
import claude_unlimited.oauth_credential as oauth_credential
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
    """Builds a fetch_account_profile fake that returns a DIFFERENT
    AccountProfile depending on which access token it's called with — needed
    to tell the "existing Profile's own credential" resolution apart from
    the "incoming login" resolution in a single test."""
    def fake(access_token, timeout=15.0):
        try:
            return mapping[access_token]
        except KeyError:
            raise anthropic_oauth.ProfileLookupError(f"no fixture for token {access_token!r}")
    return fake


# ---------------------------------------------------------------------------
# resolve_profile_own_identity(): never raises, never refreshes.
# ---------------------------------------------------------------------------

def test_resolve_profile_own_identity_none_when_no_stored_token(env):
    p = profile_repo.create_profile(name="X", kind="oauth", credential="tok-long-enough", account_uuid="uuid-1")
    env.delete_token(p.id)  # simulate the Keychain entry having vanished
    assert profile_repo.resolve_profile_own_identity(p) is None


def test_resolve_profile_own_identity_none_on_decode_error(env, monkeypatch):
    p = profile_repo.create_profile(name="X", kind="oauth", credential="tok-long-enough", account_uuid="uuid-1")

    def boom(raw):
        raise ValueError("simulated decode failure")

    monkeypatch.setattr(profile_repo.oauth_credential, "decode", boom)
    assert profile_repo.resolve_profile_own_identity(p) is None


def test_resolve_profile_own_identity_none_on_profile_lookup_error(env, monkeypatch):
    p = profile_repo.create_profile(name="X", kind="oauth", credential="tok-long-enough", account_uuid="uuid-1")

    def boom(access_token, timeout=15.0):
        raise anthropic_oauth.ProfileLookupError("token rejected")

    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", boom)
    assert profile_repo.resolve_profile_own_identity(p) is None


def test_resolve_profile_own_identity_none_on_network_error(env, monkeypatch):
    p = profile_repo.create_profile(name="X", kind="oauth", credential="tok-long-enough", account_uuid="uuid-1")

    def boom(access_token, timeout=15.0):
        raise urllib.error.URLError("no network in test")

    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", boom)
    assert profile_repo.resolve_profile_own_identity(p) is None


def test_resolve_profile_own_identity_uses_current_access_token_only(env, monkeypatch):
    # Never refreshes: a stored refresh_token/expires_at pair must not
    # trigger any refresh call, and the CURRENT access_token is what's sent.
    p = profile_repo.create_profile(
        name="X", kind="oauth", credential="tok-current-long", account_uuid="uuid-1",
        refresh_token="ref-should-never-be-used", expires_at=1)

    seen = {}

    def fake(access_token, timeout=15.0):
        seen["access_token"] = access_token
        return _account()

    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", fake)
    result = profile_repo.resolve_profile_own_identity(p)
    assert result is not None
    assert seen["access_token"] == "tok-current-long"


# ---------------------------------------------------------------------------
# The user's exact incident, as a regression test.
# ---------------------------------------------------------------------------

def test_incident_legacy_team_profile_never_overwritten_by_personal_login(env, monkeypatch):
    # A legacy Profile (org_uuid=None) whose stored credential resolves to a
    # Team organization...
    legacy = profile_repo.create_profile(
        name="dev@example.com (Team)", kind="oauth", credential="team-token-long",
        account_uuid="uuid-shared")
    assert legacy.org_uuid is None
    original_stored = env.get_token(legacy.id)

    # ...and an incoming login for the SAME account_uuid under a personal
    # Max organization — this is exactly what happened on 2026-09-22.
    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", _fetch_by_token({
        "team-token-long": _account(
            account_uuid="uuid-shared", org_uuid="org-team", org_name="Acme Inc",
            organization_type="claude_team", has_claude_max=True, has_claude_pro=False),
    }))

    new_profile, reused = profile_repo.upsert_oauth_profile(
        name="dev@example.com", account_uuid="uuid-shared", credential="personal-token-long",
        plan="max", org_uuid="org-personal-max", organization_type="claude_max")

    # TWO Profiles result.
    assert reused is False
    profiles = profile_repo.list_profiles()
    assert len(profiles) == 2
    assert new_profile.id != legacy.id

    reloaded_legacy = next(p for p in profiles if p.id == legacy.id)
    # The existing Profile's stored credential is byte-for-byte unchanged.
    assert env.get_token(legacy.id) == original_stored
    # Its name is unchanged.
    assert reloaded_legacy.name == "dev@example.com (Team)"
    # It is backfilled with its OWN team org — never the incoming login's.
    assert reloaded_legacy.org_uuid == "org-team"
    assert reloaded_legacy.organization_type == "claude_team"

    reloaded_new = next(p for p in profiles if p.id == new_profile.id)
    assert reloaded_new.org_uuid == "org-personal-max"
    assert reloaded_new.organization_type == "claude_max"
    stored_new = oauth_credential.decode(env.get_token(new_profile.id))
    assert stored_new.access_token == "personal-token-long"


# ---------------------------------------------------------------------------
# The three outcomes, directly on upsert_oauth_profile().
# ---------------------------------------------------------------------------

def test_legacy_resolves_to_same_org_reuses_and_backfills(env, monkeypatch):
    legacy = profile_repo.create_profile(
        name="Legacy", kind="oauth", credential="old-token-long", account_uuid="uuid-1")
    assert legacy.org_uuid is None

    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", _fetch_by_token({
        "old-token-long": _account(account_uuid="uuid-1", org_uuid="org-same",
                                    org_name="Acme", organization_type="claude_max"),
    }))

    updated, reused = profile_repo.upsert_oauth_profile(
        name="Legacy", account_uuid="uuid-1", credential="new-token-long",
        org_uuid="org-same", organization_type="claude_max")

    assert reused is True
    assert updated.id == legacy.id
    assert updated.org_uuid == "org-same"
    assert updated.organization_type == "claude_max"
    assert len(profile_repo.list_profiles()) == 1
    stored = oauth_credential.decode(env.get_token(legacy.id))
    assert stored.access_token == "new-token-long"  # credential DID refresh on a genuine reuse


def test_legacy_cannot_be_resolved_creates_new_profile_and_leaves_existing_untouched(env, monkeypatch):
    legacy = profile_repo.create_profile(
        name="Legacy", kind="oauth", credential="dead-token-long", account_uuid="uuid-1")
    original_stored = env.get_token(legacy.id)

    def boom(access_token, timeout=15.0):
        raise anthropic_oauth.ProfileLookupError("token no longer valid")

    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", boom)

    new_profile, reused = profile_repo.upsert_oauth_profile(
        name="Legacy", account_uuid="uuid-1", credential="fresh-token-long",
        org_uuid="org-new", organization_type="claude_max")

    assert reused is False
    assert new_profile.id != legacy.id
    profiles = profile_repo.list_profiles()
    assert len(profiles) == 2

    reloaded_legacy = next(p for p in profiles if p.id == legacy.id)
    assert env.get_token(legacy.id) == original_stored  # credential untouched
    assert reloaded_legacy.name == "Legacy"              # name untouched
    assert reloaded_legacy.org_uuid is None               # org fields untouched — never resolved
    assert reloaded_legacy.organization_type is None


# ---------------------------------------------------------------------------
# Finding 1: an unknown INCOMING organization used to bypass the guard
# entirely. find_by_account_and_org() returned needs_org_backfill=False
# whenever the incoming org_uuid argument was None — regardless of whether
# the matching Profile's own org_uuid was already known — so
# resolve_legacy_oauth_match() short-circuited and the caller overwrote the
# match with no check at all.
# ---------------------------------------------------------------------------

def test_incoming_org_unknown_and_unresolvable_never_overwrites_existing_match(env, monkeypatch):
    # This is the bug the fix closes, read literally: an existing Profile
    # for an account, and an incoming credential for the SAME account_uuid
    # whose organization is unknown and cannot be resolved. Before the fix,
    # find_by_account_and_org(account_uuid, None) returned (existing, False)
    # unconditionally, resolve_legacy_oauth_match() short-circuited on
    # needs_backfill=False, and upsert_oauth_profile() called
    # update_credential() on `existing` with no check at all — even though
    # `existing` already had a KNOWN, different-looking identity of its own.
    existing = profile_repo.create_profile(
        name="Existing", kind="oauth", credential="existing-token-long",
        account_uuid="uuid-shared", org_uuid="org-known", organization_type="claude_max")
    original_stored = env.get_token(existing.id)

    def boom(access_token, timeout=15.0):
        raise anthropic_oauth.ProfileLookupError("cannot resolve")

    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", boom)

    new_profile, reused = profile_repo.upsert_oauth_profile(
        name="Incoming", account_uuid="uuid-shared", credential="incoming-token-long",
        org_uuid=None, organization_type=None)

    assert reused is False
    assert new_profile.id != existing.id
    profiles = profile_repo.list_profiles()
    assert len(profiles) == 2

    reloaded_existing = next(p for p in profiles if p.id == existing.id)
    assert env.get_token(existing.id) == original_stored  # byte-for-byte unchanged
    assert reloaded_existing.name == "Existing"
    assert reloaded_existing.org_uuid == "org-known"       # untouched


def test_exact_pair_match_reuses_without_any_resolution_call(env, monkeypatch):
    # The genuine fast path must stay fast: when the incoming organization
    # IS known and equals a Profile's stored org_uuid, that is unambiguous
    # and must reuse without ever calling fetch_account_profile.
    existing = profile_repo.create_profile(
        name="Existing", kind="oauth", credential="existing-token-long",
        account_uuid="uuid-1", org_uuid="org-1", organization_type="claude_max")

    calls = []

    def spy(access_token, timeout=15.0):
        calls.append(access_token)
        raise anthropic_oauth.ProfileLookupError("must not be called for an exact pair match")

    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", spy)

    updated, reused = profile_repo.upsert_oauth_profile(
        name="Existing", account_uuid="uuid-1", credential="fresh-token-long",
        org_uuid="org-1", organization_type="claude_max")

    assert calls == []  # no resolution call made
    assert reused is True
    assert updated.id == existing.id
    stored = oauth_credential.decode(env.get_token(existing.id))
    assert stored.access_token == "fresh-token-long"


# ---------------------------------------------------------------------------
# POST /api/profiles — same invariant through the real daemon handler.
# ---------------------------------------------------------------------------

@pytest.fixture
def running_server(env):
    server = daemon.make_server(host="127.0.0.1", port=0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", daemon._CSRF_TOKEN
    finally:
        server.shutdown()
        t.join(timeout=2)
        server.server_close()


def _post(url, body, token):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                  headers={"X-CSRF-Token": token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_post_profiles_legacy_resolves_different_org_creates_new(env, running_server, monkeypatch):
    base, token = running_server
    legacy = profile_repo.create_profile(
        name="Legacy", kind="oauth", credential="team-token-long", account_uuid="uuid-shared")

    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", _fetch_by_token({
        "team-token-long": _account(account_uuid="uuid-shared", org_uuid="org-team",
                                     org_name="Acme", organization_type="claude_team"),
        "personal-token-long": _account(account_uuid="uuid-shared", org_uuid="org-personal-max",
                                         org_name="Personal", organization_type="claude_max"),
    }))

    status, body = _post(f"{base}/api/profiles",
                          {"name": "Personal", "kind": "oauth", "credential": "personal-token-long"}, token)
    assert status == 201  # created, not reused
    assert body["profile"]["id"] != legacy.id

    profiles = profile_repo.list_profiles()
    assert len(profiles) == 2
    reloaded_legacy = next(p for p in profiles if p.id == legacy.id)
    assert reloaded_legacy.org_uuid == "org-team"  # backfilled with ITS OWN org
    stored = oauth_credential.decode(env.get_token(legacy.id))
    assert stored.access_token == "team-token-long"  # never touched


def test_post_profiles_legacy_cannot_resolve_creates_new_non_interactively(env, running_server, monkeypatch):
    # Non-interactive path (no user to confirm with) -> "cannot resolve"
    # must mean create-new, same as add_account's default, never a silent
    # reuse.
    base, token = running_server
    legacy = profile_repo.create_profile(
        name="Legacy", kind="oauth", credential="dead-token-long", account_uuid="uuid-shared")
    original_stored = env.get_token(legacy.id)

    def fake(access_token, timeout=15.0):
        if access_token == "dead-token-long":
            raise anthropic_oauth.ProfileLookupError("expired")
        return _account(account_uuid="uuid-shared", org_uuid="org-new", organization_type="claude_max")

    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", fake)

    status, body = _post(f"{base}/api/profiles",
                          {"name": "New Login", "kind": "oauth", "credential": "new-token-long"}, token)
    assert status == 201
    assert body["profile"]["id"] != legacy.id

    profiles = profile_repo.list_profiles()
    assert len(profiles) == 2
    reloaded_legacy = next(p for p in profiles if p.id == legacy.id)
    assert env.get_token(legacy.id) == original_stored  # untouched
    assert reloaded_legacy.org_uuid is None


# ---------------------------------------------------------------------------
# POST /api/profiles — the reachable Finding-1 path: account_uuid supplied
# explicitly in the body (the paste-a-token path), org_uuid omitted. Before
# the fix, daemon.py only resolved the account when account_uuid was
# MISSING, so this branch reached find_by_account_and_org() with org_uuid=
# None and the same bypass as the direct upsert_oauth_profile() case above.
# ---------------------------------------------------------------------------

def test_post_profiles_account_uuid_supplied_no_org_and_unresolvable_creates_new(env, running_server, monkeypatch):
    base, token = running_server
    existing = profile_repo.create_profile(
        name="Existing", kind="oauth", credential="existing-token-long",
        account_uuid="uuid-shared", org_uuid="org-known", organization_type="claude_max")
    original_stored = env.get_token(existing.id)

    def boom(access_token, timeout=15.0):
        raise anthropic_oauth.ProfileLookupError("cannot resolve")

    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", boom)

    status, body = _post(f"{base}/api/profiles", {
        "name": "Incoming", "kind": "oauth", "account_uuid": "uuid-shared",
        "credential": "incoming-token-long",
    }, token)
    assert status == 201  # created, not reused
    assert body["profile"]["id"] != existing.id

    profiles = profile_repo.list_profiles()
    assert len(profiles) == 2
    reloaded_existing = next(p for p in profiles if p.id == existing.id)
    assert env.get_token(existing.id) == original_stored  # byte-for-byte unchanged
    assert reloaded_existing.org_uuid == "org-known"       # untouched


def test_post_profiles_account_uuid_supplied_no_org_resolves_and_reuses_exact_match(env, running_server, monkeypatch):
    base, token = running_server
    existing = profile_repo.create_profile(
        name="Existing", kind="oauth", credential="existing-token-long",
        account_uuid="uuid-shared", org_uuid="org-known", organization_type="claude_max")

    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", _fetch_by_token({
        "incoming-token-long": _account(account_uuid="uuid-shared", org_uuid="org-known",
                                         organization_type="claude_max"),
    }))

    status, body = _post(f"{base}/api/profiles", {
        "name": "Incoming", "kind": "oauth", "account_uuid": "uuid-shared",
        "credential": "incoming-token-long",
    }, token)
    assert status == 200
    assert body["reused_existing"] is True
    assert body["profile"]["id"] == existing.id
    assert len(profile_repo.list_profiles()) == 1
    stored = oauth_credential.decode(env.get_token(existing.id))
    assert stored.access_token == "incoming-token-long"


def test_post_profiles_account_uuid_supplied_no_org_resolves_to_different_org_creates_new(env, running_server, monkeypatch):
    base, token = running_server
    existing = profile_repo.create_profile(
        name="Existing", kind="oauth", credential="existing-token-long",
        account_uuid="uuid-shared", org_uuid="org-team", organization_type="claude_team")
    original_stored = env.get_token(existing.id)

    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", _fetch_by_token({
        "incoming-token-long": _account(account_uuid="uuid-shared", org_uuid="org-personal-max",
                                         organization_type="claude_max"),
    }))

    status, body = _post(f"{base}/api/profiles", {
        "name": "Incoming", "kind": "oauth", "account_uuid": "uuid-shared",
        "credential": "incoming-token-long",
    }, token)
    assert status == 201
    assert body["profile"]["id"] != existing.id

    profiles = profile_repo.list_profiles()
    assert len(profiles) == 2
    reloaded_existing = next(p for p in profiles if p.id == existing.id)
    assert env.get_token(existing.id) == original_stored
    assert reloaded_existing.org_uuid == "org-team"


# ---------------------------------------------------------------------------
# reauth() — the interactive path.
# ---------------------------------------------------------------------------

class _FakeCompletedProcess:
    def __init__(self, returncode=0):
        self.returncode = returncode


def _wire_reauth(monkeypatch, tmp_path, fresh_account, fresh_token="fresh-tok-long"):
    monkeypatch.setattr(cli, "CLAUDE_ACCOUNTS_DIR", tmp_path / "claude-accounts")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(cli, "_fetch_live_profiles", lambda host, port: None)
    monkeypatch.setattr(cli, "_run_tool", lambda argv, **kw: _FakeCompletedProcess(0))
    monkeypatch.setattr(anthropic_oauth, "read_claude_code_credentials",
                         lambda config_dir=None: anthropic_oauth.ImportedCredentials(
                             access_token=fresh_token, refresh_token="fresh-ref",
                             expires_at=9999, subscription_type="max"))


def test_reauth_legacy_resolves_different_org_refuses_and_writes_nothing(env, monkeypatch, tmp_path, capsys):
    target = profile_repo.create_profile(
        name="Legacy", kind="oauth", credential="team-token-long", account_uuid="uuid-shared")
    assert target.org_uuid is None
    original_stored = env.get_token(target.id)

    fresh_account = _account(account_uuid="uuid-shared", org_uuid="org-personal-max",
                              org_name="Personal Org", organization_type="claude_max")
    _wire_reauth(monkeypatch, tmp_path, fresh_account, fresh_token="personal-token-long")
    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", _fetch_by_token({
        "team-token-long": _account(account_uuid="uuid-shared", org_uuid="org-team",
                                     org_name="Acme Team", organization_type="claude_team"),
        "personal-token-long": fresh_account,
    }))

    rc = cli.reauth(cli.DEFAULT_PORT)
    assert rc == 1
    err = capsys.readouterr().err
    assert "Acme Team" in err
    assert "Personal Org" in err

    reloaded = profile_repo.find_by_account_uuid("uuid-shared")
    assert reloaded.id == target.id
    assert reloaded.org_uuid is None  # untouched — refused before any write
    assert env.get_token(target.id) == original_stored
    assert len(profile_repo.list_profiles()) == 1  # no duplicate created either


def test_reauth_legacy_cannot_resolve_confirmed_yes_reuses_and_backfills(env, monkeypatch, tmp_path, capsys):
    target = profile_repo.create_profile(
        name="Legacy", kind="oauth", credential="dead-token-long", account_uuid="uuid-shared")

    fresh_account = _account(account_uuid="uuid-shared", org_uuid="org-new",
                              org_name="New Org", organization_type="claude_max")
    _wire_reauth(monkeypatch, tmp_path, fresh_account, fresh_token="fresh-tok-long")

    def fake(access_token, timeout=15.0):
        if access_token == "dead-token-long":
            raise anthropic_oauth.ProfileLookupError("expired")
        return fresh_account

    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", fake)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    rc = cli.reauth(cli.DEFAULT_PORT)
    assert rc == 0
    assert "re-authenticated" in capsys.readouterr().out

    profiles = profile_repo.list_profiles()
    assert len(profiles) == 1
    reloaded = profiles[0]
    assert reloaded.id == target.id
    assert reloaded.org_uuid == "org-new"  # backfilled after explicit confirmation
    stored = oauth_credential.decode(env.get_token(target.id))
    assert stored.access_token == "fresh-tok-long"


def test_reauth_legacy_cannot_resolve_confirmed_no_aborts_and_writes_nothing(env, monkeypatch, tmp_path, capsys):
    target = profile_repo.create_profile(
        name="Legacy", kind="oauth", credential="dead-token-long", account_uuid="uuid-shared")
    original_stored = env.get_token(target.id)

    fresh_account = _account(account_uuid="uuid-shared", org_uuid="org-new", organization_type="claude_max")
    _wire_reauth(monkeypatch, tmp_path, fresh_account, fresh_token="fresh-tok-long")

    def fake(access_token, timeout=15.0):
        if access_token == "dead-token-long":
            raise anthropic_oauth.ProfileLookupError("expired")
        return fresh_account

    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", fake)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    rc = cli.reauth(cli.DEFAULT_PORT)
    assert rc == 1

    reloaded = profile_repo.find_by_account_uuid("uuid-shared")
    assert reloaded.org_uuid is None
    assert env.get_token(target.id) == original_stored
    assert len(profile_repo.list_profiles()) == 1


def test_reauth_legacy_cannot_resolve_eof_aborts_and_writes_nothing(env, monkeypatch, tmp_path, capsys):
    target = profile_repo.create_profile(
        name="Legacy", kind="oauth", credential="dead-token-long", account_uuid="uuid-shared")
    original_stored = env.get_token(target.id)

    fresh_account = _account(account_uuid="uuid-shared", org_uuid="org-new", organization_type="claude_max")
    _wire_reauth(monkeypatch, tmp_path, fresh_account, fresh_token="fresh-tok-long")

    def fake(access_token, timeout=15.0):
        if access_token == "dead-token-long":
            raise anthropic_oauth.ProfileLookupError("expired")
        return fresh_account

    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", fake)

    def raise_eof(prompt=""):
        raise EOFError()

    monkeypatch.setattr("builtins.input", raise_eof)

    rc = cli.reauth(cli.DEFAULT_PORT)
    assert rc == 1

    reloaded = profile_repo.find_by_account_uuid("uuid-shared")
    assert reloaded.org_uuid is None
    assert env.get_token(target.id) == original_stored
    assert len(profile_repo.list_profiles()) == 1


# ---------------------------------------------------------------------------
# Finding 3: reauth()'s overwrite guards each began with
# `if target.account_uuid and ...`, so a target Profile whose account_uuid
# is None skipped every one of them. Not reachable from current code —
# create_profile() requires account_uuid for oauth Profiles — but reachable
# from old on-disk state that predates that rule. Handled the same way as
# the "can't resolve" legacy case: an explicit interactive confirmation
# naming what is about to be recorded, and abort on anything else.
# ---------------------------------------------------------------------------

def _make_target_with_no_account_uuid(name="Ancient", credential="dead-token-long"):
    # create_profile() requires account_uuid for an oauth Profile, so this
    # simulates old on-disk state (predating that requirement) the only way
    # reachable through the public API: create with a placeholder, then blank
    # it out via update_profile() — never editing config.json by hand.
    target = profile_repo.create_profile(
        name=name, kind="oauth", credential=credential, account_uuid="placeholder-uuid")
    target = profile_repo.update_profile(target.id, account_uuid=None)
    assert target.account_uuid is None
    return target


def test_reauth_target_with_no_account_uuid_confirmed_yes_proceeds(env, monkeypatch, tmp_path, capsys):
    target = _make_target_with_no_account_uuid()

    fresh_account = _account(account_uuid="uuid-fresh", org_uuid="org-new", org_name="New Org",
                              organization_type="claude_max")
    _wire_reauth(monkeypatch, tmp_path, fresh_account, fresh_token="fresh-tok-long")
    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", lambda access_token, timeout=15.0: fresh_account)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    rc = cli.reauth(cli.DEFAULT_PORT)
    assert rc == 0
    assert "re-authenticated" in capsys.readouterr().out

    profiles = profile_repo.list_profiles()
    assert len(profiles) == 1  # no duplicate created
    reloaded = profiles[0]
    assert reloaded.id == target.id
    assert reloaded.account_uuid == "uuid-fresh"
    assert reloaded.org_uuid == "org-new"
    stored = oauth_credential.decode(env.get_token(target.id))
    assert stored.access_token == "fresh-tok-long"


def test_reauth_target_with_no_account_uuid_declined_writes_nothing(env, monkeypatch, tmp_path, capsys):
    target = _make_target_with_no_account_uuid()
    original_stored = env.get_token(target.id)

    fresh_account = _account(account_uuid="uuid-fresh", org_uuid="org-new", organization_type="claude_max")
    _wire_reauth(monkeypatch, tmp_path, fresh_account, fresh_token="fresh-tok-long")
    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", lambda access_token, timeout=15.0: fresh_account)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    rc = cli.reauth(cli.DEFAULT_PORT)
    assert rc == 1

    profiles = profile_repo.list_profiles()
    assert len(profiles) == 1
    reloaded = profiles[0]
    assert reloaded.id == target.id
    assert reloaded.account_uuid is None  # untouched — refused before any write
    assert env.get_token(target.id) == original_stored


def test_reauth_target_with_no_account_uuid_eof_writes_nothing(env, monkeypatch, tmp_path, capsys):
    target = _make_target_with_no_account_uuid()
    original_stored = env.get_token(target.id)

    fresh_account = _account(account_uuid="uuid-fresh", org_uuid="org-new", organization_type="claude_max")
    _wire_reauth(monkeypatch, tmp_path, fresh_account, fresh_token="fresh-tok-long")
    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", lambda access_token, timeout=15.0: fresh_account)

    def raise_eof(prompt=""):
        raise EOFError()

    monkeypatch.setattr("builtins.input", raise_eof)

    rc = cli.reauth(cli.DEFAULT_PORT)
    assert rc == 1

    profiles = profile_repo.list_profiles()
    assert len(profiles) == 1
    reloaded = profiles[0]
    assert reloaded.id == target.id
    assert reloaded.account_uuid is None
    assert env.get_token(target.id) == original_stored


# ---------------------------------------------------------------------------
# add_account() output.
# ---------------------------------------------------------------------------

def test_add_account_reports_displaced_legacy_profile_by_name_and_org(env, monkeypatch, tmp_path, capsys):
    legacy = profile_repo.create_profile(
        name="dev@example.com (Team)", kind="oauth", credential="team-token-long",
        account_uuid="uuid-shared")

    monkeypatch.setattr(cli, "CLAUDE_ACCOUNTS_DIR", tmp_path / "claude-accounts")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: _FakeCompletedProcess(0))

    fresh_creds = anthropic_oauth.ImportedCredentials(
        access_token="personal-token-long", refresh_token="ref", expires_at=9999, subscription_type="max")
    fresh_account = _account(account_uuid="uuid-shared", org_uuid="org-personal-max",
                              org_name="Personal Org", organization_type="claude_max")
    monkeypatch.setattr(anthropic_oauth, "read_claude_code_credentials", lambda config_dir=None: fresh_creds)
    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", _fetch_by_token({
        "team-token-long": _account(account_uuid="uuid-shared", org_uuid="org-team",
                                     org_name="Acme Team", organization_type="claude_team"),
        "personal-token-long": fresh_account,
    }))

    rc = cli.add_account()
    assert rc == 0

    out = capsys.readouterr().out
    assert "dev@example.com (Team)" in out  # names the existing Profile
    assert "Acme Team" in out               # names its organization

    profiles = profile_repo.list_profiles()
    assert len(profiles) == 2
    reloaded_legacy = next(p for p in profiles if p.id == legacy.id)
    assert reloaded_legacy.org_uuid == "org-team"
    assert oauth_credential.decode(env.get_token(legacy.id)).access_token == "team-token-long"


def test_add_account_prints_plan_team_for_a_team_login(env, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "CLAUDE_ACCOUNTS_DIR", tmp_path / "claude-accounts")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: _FakeCompletedProcess(0))

    creds = anthropic_oauth.ImportedCredentials(
        access_token="team-token-long", refresh_token="ref", expires_at=9999, subscription_type="max")
    # has_claude_max=True on a Team seat too (measured 2026-09-22) — the bug
    # this test guards was printing "Plan: Max" here from the raw flag.
    account = _account(account_uuid="uuid-team", org_uuid="org-team", org_name="Acme",
                        organization_type="claude_team", has_claude_max=True, has_claude_pro=False)
    monkeypatch.setattr(anthropic_oauth, "read_claude_code_credentials", lambda config_dir=None: creds)
    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", lambda token: account)

    assert cli.add_account() == 0
    out = capsys.readouterr().out
    assert "Plan: Team" in out
    assert "Plan: Max" not in out
