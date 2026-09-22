import pytest

import claude_unlimited.anthropic_oauth as anthropic_oauth
import claude_unlimited.cli as cli
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


class _FakeCompletedProcess:
    def __init__(self, returncode=0):
        self.returncode = returncode


def _wire_reauth_mocks(monkeypatch, tmp_path, account, login_returncode=0):
    monkeypatch.setattr(cli, "CLAUDE_ACCOUNTS_DIR", tmp_path / "claude-accounts")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(cli, "_fetch_live_profiles", lambda host, port: None)  # daemon unreachable -> show every OAuth Profile
    monkeypatch.setattr(cli, "_run_tool", lambda argv, **kw: _FakeCompletedProcess(login_returncode))
    monkeypatch.setattr(anthropic_oauth, "read_claude_code_credentials",
                         lambda config_dir=None: anthropic_oauth.ImportedCredentials(
                             access_token="fresh-tok-long", refresh_token="fresh-ref",
                             expires_at=9999, subscription_type="max"))
    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", lambda token: account)


def test_reauth_refuses_different_account(env, monkeypatch, tmp_path, capsys):
    # Genuinely different account_uuid — the pre-existing guard, must still work.
    target = profile_repo.create_profile(
        name="X", kind="oauth", credential="old-tok-long",
        account_uuid="uuid-A", org_uuid="org-A", organization_type="claude_max")

    account = anthropic_oauth.AccountProfile(
        account_uuid="uuid-B", email="x@example.com", display_name="X",
        org_uuid="org-B", org_name="Other Org", organization_type="claude_max",
        has_claude_max=True, has_claude_pro=False)
    _wire_reauth_mocks(monkeypatch, tmp_path, account)

    rc = cli.reauth(cli.DEFAULT_PORT)
    assert rc == 1
    err = capsys.readouterr().err
    assert "DIFFERENT account" in err

    reloaded = profile_repo.find_by_account_uuid("uuid-A")
    assert reloaded.id == target.id
    assert reloaded.org_uuid == "org-A"  # untouched


def test_reauth_refuses_same_account_different_org(env, monkeypatch, tmp_path, capsys):
    # The bug this ticket fixes: same account_uuid, different org_uuid — a
    # Team seat and a personal Max plan on one email. The old guard let this
    # straight through and silently swapped the credential.
    target = profile_repo.create_profile(
        name="Org Seat", kind="oauth", credential="old-tok-long",
        account_uuid="uuid-shared", org_uuid="org-team", organization_type="claude_team")

    account = anthropic_oauth.AccountProfile(
        account_uuid="uuid-shared", email="x@example.com", display_name="X",
        org_uuid="org-personal-max", org_name="X's Organization", organization_type="claude_max",
        has_claude_max=True, has_claude_pro=False)
    _wire_reauth_mocks(monkeypatch, tmp_path, account)

    rc = cli.reauth(cli.DEFAULT_PORT)
    assert rc == 1
    err = capsys.readouterr().err
    assert "DIFFERENT organization" in err or "DIFFERENT" in err

    reloaded = profile_repo.find_by_account_uuid("uuid-shared")
    assert reloaded.id == target.id
    assert reloaded.org_uuid == "org-team"  # not overwritten
    import claude_unlimited.oauth_credential as oauth_credential
    stored = oauth_credential.decode(env.get_token(target.id))
    assert stored.access_token == "old-tok-long"  # credential not swapped


def test_reauth_permits_and_backfills_when_target_org_is_none(env, monkeypatch, tmp_path, capsys):
    # A Profile added before org_uuid existed: org_uuid=None is legacy, not
    # "no organization" — matched on account_uuid alone, and the org fields
    # get backfilled rather than the login being refused or duplicated.
    target = profile_repo.create_profile(
        name="Legacy", kind="oauth", credential="old-tok-long", account_uuid="uuid-legacy")
    assert target.org_uuid is None

    account = anthropic_oauth.AccountProfile(
        account_uuid="uuid-legacy", email="legacy@example.com", display_name="Legacy",
        org_uuid="org-newly-known", org_name="Legacy's Organization", organization_type="claude_max",
        has_claude_max=True, has_claude_pro=False)
    _wire_reauth_mocks(monkeypatch, tmp_path, account)

    rc = cli.reauth(cli.DEFAULT_PORT)
    assert rc == 0
    assert "re-authenticated" in capsys.readouterr().out

    profiles = profile_repo.list_profiles()
    assert len(profiles) == 1  # no duplicate created
    reloaded = profiles[0]
    assert reloaded.id == target.id
    assert reloaded.org_uuid == "org-newly-known"          # backfilled
    assert reloaded.organization_type == "claude_max"      # backfilled

    import claude_unlimited.oauth_credential as oauth_credential
    stored = oauth_credential.decode(env.get_token(target.id))
    assert stored.access_token == "fresh-tok-long"  # credential refreshed


def test_reauth_never_renames_an_existing_profile(env, monkeypatch, tmp_path, capsys):
    # Regression guard: the user hand-named a Profile "(Team)" to tell it
    # apart from a personal-plan Profile sharing the same email, and a
    # re-auth must never overwrite that choice — even though
    # profile_name_for_account() would compute a different name ("<email>
    # (<org_name>)") for a brand-new Profile with this same account.
    target = profile_repo.create_profile(
        name="shared@example.com (Team)", kind="oauth", credential="old-tok-long",
        account_uuid="uuid-shared", org_uuid="org-team", organization_type="claude_team")

    account = anthropic_oauth.AccountProfile(
        account_uuid="uuid-shared", email="shared@example.com", display_name="Shared",
        org_uuid="org-team", org_name="Acme Inc", organization_type="claude_team",
        has_claude_max=True, has_claude_pro=False)
    _wire_reauth_mocks(monkeypatch, tmp_path, account)

    rc = cli.reauth(cli.DEFAULT_PORT)
    assert rc == 0

    profiles = profile_repo.list_profiles()
    assert len(profiles) == 1  # no duplicate created
    assert profiles[0].id == target.id
    assert profiles[0].name == "shared@example.com (Team)"  # untouched, not recomputed
