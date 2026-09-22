import json
import threading
import urllib.error
import urllib.request

import pytest

import claude_unlimited.anthropic_oauth as oauth_module
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
def running_server(monkeypatch, tmp_path):
    monkeypatch.setattr(profile_repo, "secret_store", FakeSecretStore())
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")

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


def _request(url, method="GET", body=None, headers=None):
    headers = headers or {}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_post_profile_oauth_without_account_uuid_auto_resolves_it(running_server, monkeypatch):
    base, token = running_server

    def fake_fetch(access_token, timeout=15.0):
        assert access_token == "real-oauth-token"
        return oauth_module.AccountProfile(
            account_uuid="resolved-uuid-123", email="dev@example.com", display_name="Dev",
            org_uuid=None, org_name=None, organization_type=None, has_claude_max=True, has_claude_pro=False,
        )

    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", fake_fetch)

    status, body = _request(
        f"{base}/api/profiles", "POST",
        {"name": "Personal Max", "kind": "oauth", "credential": "real-oauth-token"},
        headers={"X-CSRF-Token": token},
    )
    assert status == 201
    assert body["profile"]["account_uuid"] == "resolved-uuid-123"
    # The plan comes from the account-profile lookup, not a hardcoded default.
    assert body["profile"]["plan"] == "max"


def test_post_profile_oauth_detects_pro_plan(running_server, monkeypatch):
    base, token = running_server

    def fake_fetch(access_token, timeout=15.0):
        return oauth_module.AccountProfile(
            account_uuid="pro-uuid", email="pro@example.com", display_name="Pro",
            org_uuid=None, org_name=None, organization_type=None, has_claude_max=False, has_claude_pro=True,
        )

    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", fake_fetch)

    status, body = _request(
        f"{base}/api/profiles", "POST",
        {"name": "Personal Pro", "kind": "oauth", "credential": "real-oauth-token"},
        headers={"X-CSRF-Token": token},
    )
    assert status == 201
    assert body["profile"]["plan"] == "pro"


def test_post_profile_oauth_lookup_failure_returns_clean_400(running_server, monkeypatch):
    base, token = running_server

    def fake_fetch(access_token, timeout=15.0):
        raise oauth_module.ProfileLookupError("Anthropic rejected this token (HTTP 401)")

    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", fake_fetch)

    status, body = _request(
        f"{base}/api/profiles", "POST",
        {"name": "X", "kind": "oauth", "credential": "bad-token"},
        headers={"X-CSRF-Token": token},
    )
    assert status == 400
    assert body["error"] == "profile_lookup"


def test_post_profile_oauth_with_explicit_account_uuid_skips_lookup(running_server, monkeypatch):
    base, token = running_server

    def fail_fetch(*a, **kw):
        raise AssertionError("should not call fetch_account_profile when account_uuid is provided")

    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", fail_fetch)

    status, body = _request(
        f"{base}/api/profiles", "POST",
        {"name": "X", "kind": "oauth", "credential": "long-enough-token", "account_uuid": "manual-uuid"},
        headers={"X-CSRF-Token": token},
    )
    assert status == 201
    assert body["profile"]["account_uuid"] == "manual-uuid"


def test_post_profile_oauth_same_account_different_org_creates_second_profile(running_server, monkeypatch):
    # The core regression for this fix, exercised through the actual dashboard
    # endpoint: a Team seat and a personal Max plan on one email address
    # return the same account.uuid under a different organization.uuid, and
    # must each get their own Profile.
    base, token = running_server

    def fake_fetch_team(access_token, timeout=15.0):
        return oauth_module.AccountProfile(
            account_uuid="uuid-shared", email="dev@example.com", display_name="Dev",
            org_uuid="org-team", org_name="Acme", organization_type="claude_team",
            has_claude_max=True, has_claude_pro=False,
        )

    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", fake_fetch_team)
    status1, body1 = _request(
        f"{base}/api/profiles", "POST",
        {"name": "Org Seat", "kind": "oauth", "credential": "org-token-long"},
        headers={"X-CSRF-Token": token},
    )
    assert status1 == 201
    assert body1["profile"]["account_uuid"] == "uuid-shared"

    def fake_fetch_personal(access_token, timeout=15.0):
        return oauth_module.AccountProfile(
            account_uuid="uuid-shared", email="dev@example.com", display_name="Dev",
            org_uuid="org-personal-max", org_name="dev's Organization", organization_type="claude_max",
            has_claude_max=True, has_claude_pro=False,
        )

    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", fake_fetch_personal)
    status2, body2 = _request(
        f"{base}/api/profiles", "POST",
        {"name": "Personal Max", "kind": "oauth", "credential": "personal-token-long"},
        headers={"X-CSRF-Token": token},
    )
    assert status2 == 201  # created, not reused — did NOT overwrite the first
    assert body2["profile"]["id"] != body1["profile"]["id"]

    # The public wire dict IS a deliberate allowlist (_profile_to_public_dict
    # in daemon.py), but org_uuid/organization_type are both on it — checked
    # directly below, then persistence is checked too for good measure.
    assert body2["profile"]["org_uuid"] == "org-personal-max"
    assert body2["profile"]["organization_type"] == "claude_max"
    profiles = profile_repo.list_profiles()
    assert len(profiles) == 2  # neither Profile overwrote the other
    orgs = {p.org_uuid for p in profiles}
    assert orgs == {"org-team", "org-personal-max"}
    org_types = {p.organization_type for p in profiles}
    assert org_types == {"claude_team", "claude_max"}
    assert {p.account_uuid for p in profiles} == {"uuid-shared"}


def test_post_profile_oauth_legacy_profile_backfills_org_fields_on_reuse(running_server, monkeypatch):
    # A Profile registered before org_uuid was tracked has org_uuid=None.
    # Re-adding the same account (e.g. re-pasting a refreshed token) must
    # reuse it and backfill the org fields, not create a duplicate.
    base, token = running_server
    existing = profile_repo.create_profile(
        name="Legacy", kind="oauth", credential="old-enough-token", account_uuid="uuid-legacy")
    assert existing.org_uuid is None

    def fake_fetch(access_token, timeout=15.0):
        return oauth_module.AccountProfile(
            account_uuid="uuid-legacy", email="legacy@example.com", display_name="Legacy",
            org_uuid="org-newly-known", org_name="Legacy's Organization", organization_type="claude_max",
            has_claude_max=True, has_claude_pro=False,
        )

    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", fake_fetch)
    status, body = _request(
        f"{base}/api/profiles", "POST",
        {"name": "Legacy", "kind": "oauth", "credential": "refreshed-token-long"},
        headers={"X-CSRF-Token": token},
    )
    assert status == 200
    assert body["reused_existing"] is True
    assert body["profile"]["id"] == existing.id

    profiles = profile_repo.list_profiles()
    assert len(profiles) == 1  # no duplicate
    reloaded = profiles[0]
    assert reloaded.id == existing.id
    assert reloaded.org_uuid == "org-newly-known"          # backfilled
    assert reloaded.organization_type == "claude_max"      # backfilled


def test_import_claude_code_success_creates_profile(running_server, monkeypatch):
    base, token = running_server

    def fake_read():
        return oauth_module.ImportedCredentials(
            access_token="imported-token", refresh_token="r", expires_at=1, subscription_type="max",
        )

    def fake_fetch(access_token, timeout=15.0):
        return oauth_module.AccountProfile(
            account_uuid="imported-uuid", email="dev@example.com", display_name="Dev",
            org_uuid="org-1", org_name="Acme", organization_type=None, has_claude_max=True, has_claude_pro=False,
        )

    monkeypatch.setattr(daemon.anthropic_oauth, "read_claude_code_credentials", fake_read)
    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", fake_fetch)

    status, body = _request(f"{base}/api/import-claude-code", "POST", {}, headers={"X-CSRF-Token": token})
    assert status == 201
    assert body["profile"]["name"] == "dev@example.com"
    assert body["profile"]["account_uuid"] == "imported-uuid"
    assert body["profile"]["plan"] == "max"
    assert body["account"]["org_name"] == "Acme"


def test_import_claude_code_reimport_self_heals_missing_plan(running_server, monkeypatch):
    # A Profile whose plan was never resolved has plan=None. Re-running
    # "Import current login" must backfill it, since that flow already
    # re-fetches the account anyway.
    base, token = running_server

    def fake_read():
        return oauth_module.ImportedCredentials(
            access_token="imported-token", refresh_token="r", expires_at=1, subscription_type="max",
        )

    def fake_fetch(access_token, timeout=15.0):
        return oauth_module.AccountProfile(
            account_uuid="legacy-uuid", email="legacy@example.com", display_name="Legacy",
            org_uuid=None, org_name=None, organization_type=None, has_claude_max=True, has_claude_pro=False,
        )

    monkeypatch.setattr(daemon.anthropic_oauth, "read_claude_code_credentials", fake_read)
    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", fake_fetch)

    existing = profile_repo.create_profile(
        name="Legacy", kind="oauth", credential="old-enough-token", account_uuid="legacy-uuid",
    )
    assert existing.plan is None

    status, body = _request(f"{base}/api/import-claude-code", "POST", {}, headers={"X-CSRF-Token": token})
    assert status == 200
    assert body["reused_existing"] is True
    assert body["profile"]["plan"] == "max"
    # Regression guard: re-importing an existing Profile must never rename
    # it, even though profile_name_for_account() would compute a different
    # name ("legacy@example.com") for a brand-new Profile with this account.
    assert body["profile"]["name"] == "Legacy"


def test_import_claude_code_names_a_new_team_org_profile_with_the_org_name(running_server, monkeypatch):
    # Same defect as the CLI's add_account(): a Team seat and a personal
    # plan can share one email, so a NEW Profile from this flow must fold
    # the org name into the name too, not just the CLI path.
    base, token = running_server

    def fake_read():
        return oauth_module.ImportedCredentials(
            access_token="imported-token", refresh_token="r", expires_at=1, subscription_type="max",
        )

    def fake_fetch(access_token, timeout=15.0):
        return oauth_module.AccountProfile(
            account_uuid="team-uuid", email="shared@example.com", display_name="Shared",
            org_uuid="org-team", org_name="Acme Inc", organization_type="claude_team",
            has_claude_max=True, has_claude_pro=False,
        )

    monkeypatch.setattr(daemon.anthropic_oauth, "read_claude_code_credentials", fake_read)
    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", fake_fetch)

    status, body = _request(f"{base}/api/import-claude-code", "POST", {}, headers={"X-CSRF-Token": token})
    assert status == 201
    assert body["profile"]["name"] == "shared@example.com (Acme Inc)"
    assert body["profile"]["plan"] == "team"
    assert body["profile"]["org_uuid"] == "org-team"
    assert body["profile"]["organization_type"] == "claude_team"


def test_import_claude_code_no_local_login_returns_404(running_server, monkeypatch):
    base, token = running_server

    def fake_read():
        raise oauth_module.CredentialImportError("No Claude Code login found")

    monkeypatch.setattr(daemon.anthropic_oauth, "read_claude_code_credentials", fake_read)

    status, body = _request(f"{base}/api/import-claude-code", "POST", {}, headers={"X-CSRF-Token": token})
    assert status == 404
    assert body["error"] == "no_local_login"


def test_import_claude_code_requires_csrf(running_server):
    base, _ = running_server
    status, body = _request(f"{base}/api/import-claude-code", "POST", {})
    assert status == 403
