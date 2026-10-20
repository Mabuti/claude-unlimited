"""Tests for the Dashboard's fixed, local re-auth terminal launcher."""

import json
import shlex
import sys
import threading
from types import SimpleNamespace
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


def _capture_terminal(monkeypatch, *, platform, available, wsl=False, distro=None,
                      wslpath_output=None, wslpath_meta=None, spawn_meta=None):
    monkeypatch.setattr(daemon.sys, "platform", platform)
    monkeypatch.setattr(daemon, "_is_wsl", lambda: wsl)
    monkeypatch.setattr(daemon.shutil, "which", lambda name: available.get(name))
    if distro is None:
        monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    else:
        monkeypatch.setenv("WSL_DISTRO_NAME", distro)
    if wslpath_output is not None:
        def fake_wslpath(argv, **kwargs):
            if wslpath_meta is not None:
                wslpath_meta.update(argv=argv, kwargs=kwargs)
            return SimpleNamespace(stdout=wslpath_output)

        monkeypatch.setattr(
            daemon.subprocess,
            "run",
            fake_wslpath,
        )
    calls = []
    def capture(argv, **kwargs):
        calls.append(argv)
        if spawn_meta is not None:
            spawn_meta.update(kwargs)

    monkeypatch.setattr(daemon, "_spawn_detached", capture)
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
    assert terminal_argv[:2] == ["/usr/bin/x-terminal-emulator", "-e"]
    assert terminal_argv[2] == daemon._reauth_login_shell()
    assert terminal_argv[3] == "-lic"
    assert shlex.join([sys.executable, "-m", "claude_unlimited", "reauth", "--profile", profile.id]) in terminal_argv[4]
    assert "read -r" in terminal_argv[4]
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


def test_wsl_uses_cmd_start_without_windows_terminal(monkeypatch):
    argv = _capture_terminal(
        monkeypatch, platform="linux", wsl=True, distro="Ubuntu-22.04",
        available={"cmd.exe": "/mnt/c/Windows/System32/cmd.exe", "wt.exe": "/mnt/c/Windows/wt.exe"},
    )

    assert argv[:9] == [
        "/mnt/c/Windows/System32/cmd.exe", "/c", "start", "", "wsl.exe",
        "-d", "Ubuntu-22.04", "--exec", daemon._reauth_login_shell(),
    ]
    assert argv[9] == "-lic"
    assert shlex.join([sys.executable, "-m", "claude_unlimited", "reauth", "--profile", "deadbeef"]) in argv[10]
    assert all(item != "wt.exe" for item in argv)


def test_wsl_falls_back_to_cmd_start(monkeypatch):
    argv = _capture_terminal(
        monkeypatch, platform="linux", wsl=True, distro="Ubuntu",
        available={"cmd.exe": "/mnt/c/Windows/System32/cmd.exe"},
    )

    assert argv[:10] == [
        "/mnt/c/Windows/System32/cmd.exe", "/c", "start", "", "wsl.exe", "-d", "Ubuntu", "--exec",
        daemon._reauth_login_shell(), "-lic",
    ]
    assert argv[-1] == argv[10]
    assert shlex.join([sys.executable, "-m", "claude_unlimited", "reauth", "--profile", "deadbeef"]) in argv[-1]


def test_wsl_rejects_interpreter_path_with_spaces_without_spawning(monkeypatch):
    monkeypatch.setattr(daemon.sys, "platform", "linux")
    monkeypatch.setattr(daemon.sys, "executable", "/opt/Python With Space/bin/python")
    monkeypatch.setattr(daemon, "_is_wsl", lambda: True)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    spawned = []
    monkeypatch.setattr(daemon, "_spawn_detached", lambda *args, **kwargs: spawned.append((args, kwargs)))

    launched, reason = daemon._launch_reauth_terminal("deadbeef")

    assert launched is False
    assert "interpreter path cannot be passed through Windows safely" in reason
    assert spawned == []


def test_native_windows_uses_wt_when_available(monkeypatch):
    argv = _capture_terminal(
        monkeypatch, platform="win32",
        available={"wt.exe": r"C:\\Windows\\wt.exe"},
    )

    assert argv == [
        r"C:\\Windows\\wt.exe", "cmd.exe", "/k", sys.executable, "-m", "claude_unlimited",
        "reauth", "--profile", "deadbeef",
    ]


def test_native_windows_cmd_fallback_keeps_window_open(monkeypatch):
    argv = _capture_terminal(
        monkeypatch, platform="win32",
        available={"cmd.exe": r"C:\\Windows\\System32\\cmd.exe"},
    )

    assert argv == [
        r"C:\\Windows\\System32\\cmd.exe", "/c", "start", "", "cmd.exe", "/k",
        sys.executable, "-m", "claude_unlimited", "reauth", "--profile", "deadbeef",
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
        "/usr/bin/gnome-terminal", "--", daemon._reauth_login_shell(), "-lic", argv[-1],
    ]
    assert shlex.join([sys.executable, "-m", "claude_unlimited", "reauth", "--profile", "deadbeef"]) in argv[-1]


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


@pytest.mark.parametrize(
    ("wslpath_output", "expected_distro"),
    [
        ("\\\\wsl.localhost\\Ubuntu\\", "Ubuntu"),
        ("\\\\wsl$\\Ubuntu-22.04\\", "Ubuntu-22.04"),
    ],
)
def test_wsl_resolves_distro_from_wslpath_when_environment_is_missing(
    monkeypatch, wslpath_output, expected_distro
):
    wslpath_meta = {}
    spawn_meta = {}
    argv = _capture_terminal(
        monkeypatch,
        platform="linux",
        wsl=True,
        available={
            "wslpath": "/usr/bin/wslpath",
            "cmd.exe": "/mnt/c/Windows/System32/cmd.exe",
            "wt.exe": "/mnt/c/Users/test/AppData/Local/Microsoft/WindowsApps/wt.exe",
        },
        distro=None,
        wslpath_output=wslpath_output,
        wslpath_meta=wslpath_meta,
        spawn_meta=spawn_meta,
    )

    assert argv[4:7] == ["wsl.exe", "-d", expected_distro]
    assert "-d" in argv
    assert wslpath_meta == {
        "argv": ["/usr/bin/wslpath", "-w", "/"],
        "kwargs": {"capture_output": True, "text": True, "timeout": 2, "check": True},
    }
    assert spawn_meta == {"cwd": "/mnt/c/Windows"}


def test_wsl_without_a_distro_falls_back_without_spawning(monkeypatch):
    monkeypatch.setattr(daemon.sys, "platform", "linux")
    monkeypatch.setattr(daemon, "_is_wsl", lambda: True)
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.setattr(daemon.shutil, "which", lambda name: {
        "wslpath": "/usr/bin/wslpath",
        "cmd.exe": "/mnt/c/Windows/System32/cmd.exe",
        "wt.exe": "/mnt/c/Windows/wt.exe",
    }.get(name))
    monkeypatch.setattr(daemon.subprocess, "run", lambda argv, **kwargs: SimpleNamespace(stdout="not-a-wsl-path"))
    spawned = []
    monkeypatch.setattr(daemon, "_spawn_detached", lambda *args, **kwargs: spawned.append((args, kwargs)))

    launched, reason = daemon._launch_reauth_terminal("deadbeef")

    assert launched is False
    assert reason == "Could not determine the WSL distribution."
    assert spawned == []


def test_wsl_uses_fixed_cmd_path_when_cmd_is_not_on_path(monkeypatch):
    monkeypatch.setattr(daemon.os.path, "isfile",
                        lambda path: path == "/mnt/c/Windows/System32/cmd.exe")
    argv = _capture_terminal(
        monkeypatch,
        platform="linux",
        wsl=True,
        distro="Ubuntu",
        available={
            "wt.exe": "/mnt/c/Users/test/AppData/Local/Microsoft/WindowsApps/wt.exe",
        },
    )

    assert argv[0] == "/mnt/c/Windows/System32/cmd.exe"
    assert argv[4] == "wsl.exe"


def test_wsl_cmd_uses_wslpath_for_custom_automount_root(monkeypatch):
    wslpath_meta = {}
    monkeypatch.setattr(daemon.shutil, "which", lambda name: {
        "wslpath": "/usr/bin/wslpath",
    }.get(name))

    def fake_run(argv, **kwargs):
        wslpath_meta.update(argv=argv, kwargs=kwargs)
        return SimpleNamespace(stdout="/custom/windows/System32/cmd.exe\n")

    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    monkeypatch.setattr(daemon.os.path, "isfile",
                        lambda path: path == "/custom/windows/System32/cmd.exe")

    assert daemon._wsl_cmd_executable() == "/custom/windows/System32/cmd.exe"
    assert wslpath_meta == {
        "argv": ["/usr/bin/wslpath", "-u", r"C:\Windows\System32\cmd.exe"],
        "kwargs": {"capture_output": True, "text": True, "timeout": 2, "check": True},
    }


def test_linux_launcher_uses_the_login_interactive_shell_and_keeps_window_open(monkeypatch):
    monkeypatch.setattr(daemon, "pwd", SimpleNamespace(
        getpwuid=lambda uid: SimpleNamespace(pw_shell="/bin/zsh"),
    ))
    monkeypatch.setattr(daemon.os, "getuid", lambda: 123)
    argv = _capture_terminal(
        monkeypatch,
        platform="linux",
        available={"x-terminal-emulator": "/usr/bin/x-terminal-emulator"},
    )

    assert argv[:4] == ["/usr/bin/x-terminal-emulator", "-e", "/bin/zsh", "-lic"]
    assert shlex.join([sys.executable, "-m", "claude_unlimited", "reauth", "--profile", "deadbeef"]) in argv[4]
    assert "status=$?" in argv[4]
    assert '"' not in argv[4]
    assert "read -r" in argv[4]
    assert "attacker" not in argv[4]


@pytest.mark.parametrize(
    ("configured_shell", "expected_shell"),
    [
        ("/usr/bin/fish", "/bin/bash"),
        ("/usr/bin/zsh", "/usr/bin/zsh"),
    ],
)
def test_reauth_login_shell_allows_only_supported_shells(monkeypatch, configured_shell, expected_shell):
    monkeypatch.setattr(daemon, "pwd", SimpleNamespace(
        getpwuid=lambda uid: SimpleNamespace(pw_shell=configured_shell),
    ))
    monkeypatch.setattr(daemon.os, "getuid", lambda: 123)

    assert daemon._reauth_login_shell() == expected_shell


def test_native_windows_replaces_pythonw_with_sibling_python(monkeypatch, tmp_path):
    pythonw = tmp_path / "pythonw.exe"
    python = tmp_path / "python.exe"
    pythonw.touch()
    python.touch()
    monkeypatch.setattr(daemon.sys, "platform", "win32")
    monkeypatch.setattr(daemon.sys, "executable", str(pythonw))
    monkeypatch.setattr(daemon.shutil, "which", lambda name: r"C:\\Windows\\wt.exe" if name == "wt.exe" else None)
    calls = []
    monkeypatch.setattr(daemon, "_spawn_detached", lambda argv: calls.append(argv))

    launched, reason = daemon._launch_reauth_terminal("deadbeef")

    assert launched is True
    assert reason is None
    assert calls == [[r"C:\\Windows\\wt.exe", "cmd.exe", "/k", str(python), "-m", "claude_unlimited",
                      "reauth", "--profile", "deadbeef"]]


def test_native_windows_pythonw_without_sibling_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon.sys, "platform", "win32")
    monkeypatch.setattr(daemon.sys, "executable", str(tmp_path / "pythonw.exe"))
    monkeypatch.setattr(daemon, "_spawn_detached", lambda argv: pytest.fail("must not spawn"))

    launched, reason = daemon._launch_reauth_terminal("deadbeef")

    assert launched is False
    assert reason == "pythonw.exe has no sibling python.exe for terminal re-auth."
