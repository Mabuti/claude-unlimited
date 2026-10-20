"""Tests for the Dashboard's fixed, local re-auth terminal launcher."""

import json
import shlex
import sys
import threading
import urllib.error
import urllib.request

import pytest

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
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")

    server = daemon.make_server(host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", daemon._CSRF_TOKEN
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _post(url, token, body=None):
    data = json.dumps(body if body is not None else {}).encode()
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "X-CSRF-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _oauth_profile():
    return profile_repo.create_profile(
        name="Claude", kind="oauth", credential="oauth-token-long",
        account_uuid="account-uuid", org_uuid="org-uuid", organization_type="claude_max",
    )


def _capture_terminal(monkeypatch, *, platform, available, wsl=False, distro=None):
    monkeypatch.setattr(daemon.sys, "platform", platform)
    monkeypatch.setattr(daemon, "_is_wsl", lambda: wsl)
    monkeypatch.setattr(daemon.shutil, "which", lambda name: available.get(name))
    if distro is None:
        monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    else:
        monkeypatch.setenv("WSL_DISTRO_NAME", distro)
    calls = []
    monkeypatch.setattr(daemon, "_spawn_detached", lambda argv: calls.append(argv))
    launched, reason = daemon._launch_reauth_terminal("deadbeef")
    assert launched is True
    assert reason is None
    assert len(calls) == 1
    return calls[0]


def test_unknown_profile_id_is_404(running_server):
    base, token = running_server

    status, body = _post(f"{base}/api/profiles/deadbeef/reauth/launch", token)

    assert status == 404
    assert body["error"] == "not_found"


def test_malformed_profile_id_is_rejected_before_pool_lookup(running_server, monkeypatch):
    base, token = running_server
    monkeypatch.setattr(daemon.profile_repo, "list_profiles",
                        lambda: pytest.fail("malformed ids must not load the pool"))
    monkeypatch.setattr(daemon, "_launch_reauth_terminal",
                        lambda profile_id: pytest.fail("malformed ids must not launch a terminal"))

    status, body = _post(f"{base}/api/profiles/not-a-profile/reauth/launch", token)

    assert status == 400
    assert body["error"] == "bad_request"


def test_non_oauth_profile_id_is_400(running_server):
    base, token = running_server
    profile = profile_repo.create_profile(name="API", kind="api", credential="api-token-long")

    status, body = _post(f"{base}/api/profiles/{profile.id}/reauth/launch", token)

    assert status == 400
    assert body["error"] == "not_oauth"


def test_successful_launch_uses_only_validated_path_id(running_server, monkeypatch):
    base, token = running_server
    profile = _oauth_profile()
    calls = []
    monkeypatch.setattr(daemon, "_launch_reauth_terminal",
                        lambda profile_id: calls.append(profile_id) or (True, None))

    status, body = _post(
        f"{base}/api/profiles/{profile.id}/reauth/launch", token,
        {"profile_id": "attacker", "command": ["rm", "-rf", "/"], "argv": ["evil"]},
    )

    assert status == 200
    assert body == {"launched": True}
    assert calls == [profile.id]


def test_endpoint_builds_fixed_argv_ignoring_request_fields(running_server, monkeypatch):
    base, token = running_server
    profile = _oauth_profile()
    monkeypatch.setattr(daemon.sys, "platform", "linux")
    monkeypatch.setattr(daemon, "_is_wsl", lambda: False)
    monkeypatch.setattr(daemon.shutil, "which",
                        lambda name: "/usr/bin/x-terminal-emulator" if name == "x-terminal-emulator" else None)
    terminal_argv = []
    monkeypatch.setattr(daemon, "_spawn_detached", lambda argv: terminal_argv.extend(argv))

    status, body = _post(
        f"{base}/api/profiles/{profile.id}/reauth/launch", token,
        {"profile_id": "attacker", "command": "attacker-command"},
    )

    assert status == 200
    assert body["launched"] is True
    assert terminal_argv == [
        "/usr/bin/x-terminal-emulator", "-e", sys.executable, "-m", "claude_unlimited",
        "reauth", "--profile", profile.id,
    ]
    assert "attacker-command" not in terminal_argv


def test_spawn_failure_returns_copyable_fallback(running_server, monkeypatch):
    base, token = running_server
    profile = _oauth_profile()
    monkeypatch.setattr(daemon, "_launch_reauth_terminal",
                        lambda profile_id: (False, "launcher exploded"))

    status, body = _post(f"{base}/api/profiles/{profile.id}/reauth/launch", token)

    assert status == 200
    assert body == {
        "launched": False,
        "command": f"claude-unlimited reauth --profile {profile.id}",
        "reason": "launcher exploded",
    }


def test_wsl_uses_windows_terminal_and_wsl_distro(monkeypatch):
    argv = _capture_terminal(
        monkeypatch, platform="linux", wsl=True, distro="Ubuntu-22.04",
        available={"wt.exe": "/mnt/c/Windows/wt.exe"},
    )

    assert argv == [
        "/mnt/c/Windows/wt.exe", "wsl.exe", "-d", "Ubuntu-22.04", "--",
        sys.executable, "-m", "claude_unlimited", "reauth", "--profile", "deadbeef",
    ]


def test_wsl_falls_back_to_cmd_start(monkeypatch):
    argv = _capture_terminal(
        monkeypatch, platform="linux", wsl=True, distro="Ubuntu",
        available={"cmd.exe": "/mnt/c/Windows/System32/cmd.exe"},
    )

    assert argv == [
        "/mnt/c/Windows/System32/cmd.exe", "/c", "start", "", "wsl.exe", "-d", "Ubuntu", "--",
        sys.executable, "-m", "claude_unlimited", "reauth", "--profile", "deadbeef",
    ]


def test_native_windows_uses_wt_when_available(monkeypatch):
    argv = _capture_terminal(
        monkeypatch, platform="win32",
        available={"wt.exe": r"C:\\Windows\\wt.exe"},
    )

    assert argv == [
        r"C:\\Windows\\wt.exe", sys.executable, "-m", "claude_unlimited",
        "reauth", "--profile", "deadbeef",
    ]


def test_macos_uses_terminal_do_script_with_shell_quoted_command(monkeypatch):
    argv = _capture_terminal(
        monkeypatch, platform="darwin", available={"osascript": "/usr/bin/osascript"},
    )

    assert argv[:2] == ["/usr/bin/osascript", "-e"]
    assert 'tell application "Terminal" to do script ' in argv[2]
    assert shlex.join([sys.executable, "-m", "claude_unlimited", "reauth", "--profile", "deadbeef"]) in argv[2]


def test_linux_uses_first_available_desktop_terminal(monkeypatch):
    argv = _capture_terminal(
        monkeypatch, platform="linux",
        available={"x-terminal-emulator": None, "gnome-terminal": "/usr/bin/gnome-terminal"},
    )

    assert argv == [
        "/usr/bin/gnome-terminal", "--", sys.executable, "-m", "claude_unlimited",
        "reauth", "--profile", "deadbeef",
    ]


def test_spawn_error_is_reported_as_launcher_failure(monkeypatch):
    monkeypatch.setattr(daemon.sys, "platform", "linux")
    monkeypatch.setattr(daemon, "_is_wsl", lambda: False)
    monkeypatch.setattr(daemon.shutil, "which",
                        lambda name: "/usr/bin/xterm" if name == "xterm" else None)

    def fail(argv):
        raise OSError("permission denied")

    monkeypatch.setattr(daemon, "_spawn_detached", fail)

    launched, reason = daemon._launch_reauth_terminal("deadbeef")

    assert launched is False
    assert reason == "permission denied"
