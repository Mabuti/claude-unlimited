import io
import json
import subprocess
import urllib.error

import pytest

import claude_unlimited.anthropic_oauth as oauth_module
from claude_unlimited.anthropic_oauth import (
    AccountProfile,
    CredentialImportError,
    ProfileLookupError,
    fetch_account_profile,
    isolated_macos_keychain_service,
    plan_from_account,
    profile_name_for_account,
    read_claude_code_credentials,
)


def test_isolated_macos_keychain_service_matches_the_algorithm_claude_code_actually_uses():
    # Pins the suffix algorithm Claude Code uses for an isolated config dir:
    # sha256(path).hexdigest()[:8]. A failure here means Claude Code changed
    # how it derives the service name, and the isolated-login fallback needs
    # re-deriving rather than just re-pinning.
    config_dir = "/home/example-user/.claude-unlimited/claude-accounts/aaaaaaaaaaaaaaaa"
    assert isolated_macos_keychain_service(config_dir) == "Claude Code-credentials-b5f03c19"


def test_reads_credentials_from_file_when_present(tmp_path, monkeypatch):
    cred_file = tmp_path / ".credentials.json"
    cred_file.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "tok-abc", "refreshToken": "ref-abc",
                           "expiresAt": 123, "subscriptionType": "max"},
    }))
    monkeypatch.setattr(oauth_module, "DEFAULT_CREDENTIALS_PATH", cred_file)

    creds = read_claude_code_credentials()
    assert creds.access_token == "tok-abc"
    assert creds.subscription_type == "max"


def test_falls_back_to_macos_keychain_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(oauth_module, "DEFAULT_CREDENTIALS_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(oauth_module.platform, "system", lambda: "Darwin")

    def fake_run(cmd, **kw):
        assert cmd[:3] == ["security", "find-generic-password", "-s"]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
            "claudeAiOauth": {"accessToken": "tok-from-keychain"},
        }), stderr="")

    monkeypatch.setattr(oauth_module.subprocess, "run", fake_run)
    creds = read_claude_code_credentials()
    assert creds.access_token == "tok-from-keychain"


def test_raises_clear_error_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.setattr(oauth_module, "DEFAULT_CREDENTIALS_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(oauth_module.platform, "system", lambda: "Darwin")

    def fake_run(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(oauth_module.subprocess, "run", fake_run)
    with pytest.raises(CredentialImportError):
        read_claude_code_credentials()


def test_non_darwin_with_no_file_raises_without_trying_keychain(tmp_path, monkeypatch):
    monkeypatch.setattr(oauth_module, "DEFAULT_CREDENTIALS_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(oauth_module.platform, "system", lambda: "Linux")

    def fail_run(cmd, **kw):
        raise AssertionError("should never call `security` on non-macOS")

    monkeypatch.setattr(oauth_module.subprocess, "run", fail_run)
    with pytest.raises(CredentialImportError):
        read_claude_code_credentials()


def test_reads_credentials_from_an_isolated_config_dir_when_given(tmp_path, monkeypatch):
    # A different CLAUDE_CONFIG_DIR (see cli.py's `add_account`) must never
    # fall back to the default location or Keychain — that would silently
    # merge an unrelated account's credential into this one with no sign
    # anything went wrong.
    def fail_run(cmd, **kw):
        raise AssertionError("must never touch Keychain when an isolated config_dir is given")

    monkeypatch.setattr(oauth_module.subprocess, "run", fail_run)

    isolated_dir = tmp_path / "isolated-account"
    isolated_dir.mkdir()
    (isolated_dir / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "tok-isolated", "refreshToken": "ref-isolated",
                           "expiresAt": 456, "subscriptionType": "pro"},
    }))

    creds = read_claude_code_credentials(config_dir=isolated_dir)
    assert creds.access_token == "tok-isolated"
    assert creds.subscription_type == "pro"


def test_isolated_config_dir_falls_back_to_the_suffixed_macos_keychain_entry(tmp_path, monkeypatch):
    # On macOS, Claude Code writes no .credentials.json file for an isolated
    # CLAUDE_CONFIG_DIR — it uses Keychain under a directory-derived service
    # name (see isolated_macos_keychain_service).
    isolated_dir = tmp_path / "isolated-account"
    isolated_dir.mkdir()  # no .credentials.json written — forces the Keychain fallback
    expected_service = oauth_module.isolated_macos_keychain_service(isolated_dir)

    monkeypatch.setattr(oauth_module.platform, "system", lambda: "Darwin")

    def fake_run(cmd, **kw):
        assert cmd[:3] == ["security", "find-generic-password", "-s"]
        service_used = cmd[3]
        assert service_used == expected_service
        assert service_used != oauth_module.MACOS_KEYCHAIN_SERVICE  # never the UN-suffixed default service
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
            "claudeAiOauth": {"accessToken": "tok-isolated-keychain"},
        }), stderr="")

    monkeypatch.setattr(oauth_module.subprocess, "run", fake_run)

    creds = read_claude_code_credentials(config_dir=isolated_dir)
    assert creds.access_token == "tok-isolated-keychain"


def test_isolated_config_dir_with_nothing_found_anywhere_raises_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(oauth_module.platform, "system", lambda: "Darwin")

    def fake_run(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)  # matches security's "not found" exit

    monkeypatch.setattr(oauth_module.subprocess, "run", fake_run)

    isolated_dir = tmp_path / "isolated-account-empty"
    isolated_dir.mkdir()
    with pytest.raises(CredentialImportError, match="CLAUDE_CONFIG_DIR"):
        read_claude_code_credentials(config_dir=isolated_dir)


def test_isolated_config_dir_on_non_darwin_with_no_file_raises_without_trying_keychain(tmp_path, monkeypatch):
    monkeypatch.setattr(oauth_module.platform, "system", lambda: "Linux")

    def fail_run(cmd, **kw):
        raise AssertionError("should never call `security` on non-macOS")

    monkeypatch.setattr(oauth_module.subprocess, "run", fail_run)

    isolated_dir = tmp_path / "isolated-account-empty"
    isolated_dir.mkdir()
    with pytest.raises(CredentialImportError, match="CLAUDE_CONFIG_DIR"):
        read_claude_code_credentials(config_dir=isolated_dir)


class _FakeHTTPResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_fetch_account_profile_parses_real_response_shape(monkeypatch):
    payload = json.dumps({
        "account": {"uuid": "acct-1234", "email": "dev@example.com", "display_name": "Dev",
                     "has_claude_max": True, "has_claude_pro": False},
        "organization": {"uuid": "org-5678", "name": "Acme", "organization_type": "claude_max"},
    }).encode()

    def fake_urlopen(req, timeout=None):
        assert req.get_header("Authorization") == "Bearer real-token"
        return _FakeHTTPResponse(payload)

    monkeypatch.setattr(oauth_module.urllib.request, "urlopen", fake_urlopen)
    profile = fetch_account_profile("real-token")
    assert profile.account_uuid == "acct-1234"
    assert profile.org_uuid == "org-5678"
    assert profile.organization_type == "claude_max"
    assert profile.has_claude_max is True


def test_fetch_account_profile_parses_organization_type_for_a_team_seat(monkeypatch):
    # Measured 2026-09-22 (spec §4): a Team seat's organization_type is
    # "claude_team", and has_claude_max is true there too — organization_type
    # is the only field in this payload that tells the two apart.
    payload = json.dumps({
        "account": {"uuid": "acct-team", "email": "dev@example.com", "display_name": "Dev",
                     "has_claude_max": True, "has_claude_pro": False},
        "organization": {"uuid": "org-team", "name": "ExeVision Inc.", "organization_type": "claude_team"},
    }).encode()

    def fake_urlopen(req, timeout=None):
        return _FakeHTTPResponse(payload)

    monkeypatch.setattr(oauth_module.urllib.request, "urlopen", fake_urlopen)
    profile = fetch_account_profile("real-token")
    assert profile.organization_type == "claude_team"


def _account(organization_type=None, has_claude_max=False, has_claude_pro=False, email=None, org_name=None):
    return AccountProfile(
        account_uuid="acct", email=email, display_name=None, org_uuid=None, org_name=org_name,
        organization_type=organization_type, has_claude_max=has_claude_max, has_claude_pro=has_claude_pro,
    )


def test_plan_from_account_returns_team_for_a_team_organization():
    # has_claude_max is true on a Team seat too (spec §4 result 2) — this
    # asserts the team branch wins over the flags, not just that it exists.
    account = _account(organization_type="claude_team", has_claude_max=True)
    assert plan_from_account(account) == "team"


def test_plan_from_account_returns_max_when_flag_set_and_no_team_org():
    account = _account(organization_type="claude_max", has_claude_max=True)
    assert plan_from_account(account) == "max"


def test_plan_from_account_returns_pro():
    account = _account(organization_type="claude_max", has_claude_pro=True)
    assert plan_from_account(account) == "pro"


def test_plan_from_account_returns_none_when_nothing_determinable():
    account = _account()
    assert plan_from_account(account) is None


def test_plan_from_account_falls_back_to_flags_for_an_unrecognised_organization_type():
    # An organization_type this code doesn't recognise (e.g. a future
    # claude_enterprise) must never be guessed at — fall through to the
    # existing flag logic rather than inventing a label.
    account = _account(organization_type="claude_enterprise", has_claude_max=True)
    assert plan_from_account(account) == "max"


def test_profile_name_for_account_is_plain_email_for_a_personal_max_plan():
    account = _account(organization_type="claude_max", email="user@example.com", org_name="Personal Org")
    assert profile_name_for_account(account) == "user@example.com"


def test_profile_name_for_account_is_plain_email_for_a_personal_pro_plan():
    account = _account(organization_type="claude_pro", email="user@example.com", org_name="Personal Org")
    assert profile_name_for_account(account) == "user@example.com"


def test_profile_name_for_account_includes_org_name_for_a_team_organization():
    account = _account(organization_type="claude_team", email="user@example.com", org_name="Acme Inc")
    assert profile_name_for_account(account) == "user@example.com (Acme Inc)"


def test_profile_name_for_account_falls_back_to_plain_email_when_organization_type_is_none():
    account = _account(organization_type=None, email="user@example.com", org_name="Acme Inc")
    assert profile_name_for_account(account) == "user@example.com"


def test_profile_name_for_account_falls_back_to_plain_email_when_org_name_missing():
    # organization_type says this isn't personal, but there's nothing to put
    # in the parens — must never render a dangling "()".
    account = _account(organization_type="claude_team", email="user@example.com", org_name=None)
    assert profile_name_for_account(account) == "user@example.com"


def test_profile_name_for_account_falls_back_to_imported_placeholder_when_email_missing():
    account = _account(organization_type="claude_team", email=None, org_name="Acme Inc")
    assert profile_name_for_account(account) == "Imported Claude account"


def test_fetch_account_profile_raises_on_missing_uuid(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeHTTPResponse(json.dumps({"account": {}}).encode())

    monkeypatch.setattr(oauth_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ProfileLookupError):
        fetch_account_profile("real-token")


def test_fetch_account_profile_raises_clear_error_on_http_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(oauth_module.PROFILE_URL, 401, "unauthorized",
                                      hdrs=None, fp=io.BytesIO(b'{"error":{"message":"bad token"}}'))

    monkeypatch.setattr(oauth_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ProfileLookupError):
        fetch_account_profile("bad-token")


def test_fetch_account_profile_gives_a_friendly_message_for_setup_token_scope_error(monkeypatch):
    # A `claude setup-token` token is scoped for headless inference only and
    # can never complete this lookup, so the message must say that plainly
    # instead of dumping the raw Anthropic error JSON.
    real_403_body = (
        b'{"type":"error","error":{"type":"permission_error",'
        b'"message":"OAuth token does not meet scope requirement '
        b'any_of(user:profile, user:office)","details":{"error_visibility":"user_facing"}},'
        b'"request_id":"req_011CeEYqHbYHTVq7HyfczvBt"}'
    )

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(oauth_module.PROFILE_URL, 403, "forbidden",
                                      hdrs=None, fp=io.BytesIO(real_403_body))

    monkeypatch.setattr(oauth_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ProfileLookupError) as exc_info:
        fetch_account_profile("setup-token-scoped-token")

    message = str(exc_info.value)
    assert "claude setup-token" in message
    assert "Import current login" in message
    assert "claude-unlimited add-account" in message
    # Never leak the raw Anthropic error JSON into what the user sees.
    assert "permission_error" not in message
    assert "request_id" not in message


def test_fetch_account_profile_still_raises_generic_message_for_other_403s(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(oauth_module.PROFILE_URL, 403, "forbidden",
                                      hdrs=None, fp=io.BytesIO(b'{"error":{"message":"account suspended"}}'))

    monkeypatch.setattr(oauth_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ProfileLookupError) as exc_info:
        fetch_account_profile("some-token")

    # A 403 for an unrelated reason (no "scope requirement" in the body)
    # must not be swallowed into the scope-specific message.
    assert "setup-token" not in str(exc_info.value)
