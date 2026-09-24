import errno
import types

import os
import pytest

import claude_unlimited.cli as cli
import claude_unlimited.daemon_installer as daemon_installer
from claude_unlimited import __version__


def _stub_launch(monkeypatch, sink=None):
    """Stubs both ways `code()` launches `claude`: POSIX `os.execvp` (never
    returns on success) and the Windows child-process fallback, `_run_tool`
    (see cli.py's `if os.name == "nt"` branch in `code()`). Only one of the
    two ever actually runs for a given OS, but stubbing just `execvp` leaves
    Windows falling through to a real subprocess launch of a fake path.
    `sink`, if given, records each call as (binary, argv) like the old
    execvp-only stubs did."""
    def execvp(cmd, args):
        if sink is not None:
            sink.append((cmd, args))

    def run_tool(argv, **kw):
        if sink is not None:
            sink.append((cli._resolve_launcher(argv[0]), argv))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.os, "execvp", execvp)
    monkeypatch.setattr(cli, "_run_tool", run_tool)


def _serving(monkeypatch, *versions):
    """Scripts what /health reports on successive probes; the last value
    repeats. None means nothing is answering."""
    seq = list(versions)

    def probe(host, port, timeout=1.0):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(cli, "_running_version", probe)


def test_start_when_already_running_is_a_friendly_noop(monkeypatch, capsys):
    # A second `claude-unlimited start` must recognize its own daemon and exit
    # cleanly, not fail to bind the port.
    _serving(monkeypatch, __version__)
    calls = []
    monkeypatch.setattr(cli, "run_foreground", lambda **kw: calls.append(kw))
    assert cli.main(["start"]) == 0
    assert calls == []  # never even tried to bind
    out = capsys.readouterr().out
    assert "Already running" in out


def test_start_port_in_use_by_something_else_is_a_friendly_error(monkeypatch, capsys):
    _serving(monkeypatch, None)

    def boom(**kw):
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(cli, "run_foreground", boom)
    assert cli.main(["start"]) == 1
    err = capsys.readouterr().err
    assert "already in use" in err.lower()
    assert "Traceback" not in err


def test_start_refuses_to_noop_over_a_different_version(monkeypatch, capsys):
    # The upgrade failure the user hit: files on disk are new, the daemon
    # answering is old, and "nothing more to do" hid it.
    _serving(monkeypatch, "0.0.1-old")
    monkeypatch.setattr(cli, "run_foreground", lambda **kw: None)
    assert cli.main(["start"]) == 1
    out = capsys.readouterr().out
    assert "0.0.1-old" in out and "restart" in out.lower()


def test_start_reraises_unrelated_os_errors(monkeypatch):
    _serving(monkeypatch, None)

    def boom(**kw):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(cli, "run_foreground", boom)
    with pytest.raises(OSError):
        cli.main(["start"])


def test_code_missing_claude_binary_fails_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["code"]) == 1
    assert "not found on PATH" in capsys.readouterr().err


def test_code_when_daemon_already_running_execs_claude_directly(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(cli, "_probe_health", lambda host, port, timeout=1.0: True)
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port: "tok-123")
    spawn_calls = []
    monkeypatch.setattr(cli, "_spawn_background_daemon", lambda port: spawn_calls.append(port))
    exec_calls = []
    _stub_launch(monkeypatch, exec_calls)

    assert cli.main(["code", "--model", "opus"]) == 0

    assert spawn_calls == []  # already running — never tried to start a second one
    # A --settings status-line hint may be prepended (see cli._status_line_args);
    # what matters is that claude is exec'd with the user's own args intact.
    (binary, argv), = exec_calls
    assert os.path.basename(binary) == "claude"  # resolved path, argv[0] stays the bare name
    assert argv[0] == "claude" and argv[-2:] == ["--model", "opus"]
    assert cli.os.environ["ANTHROPIC_BASE_URL"] == f"http://{cli.LOOPBACK_HOST}:{cli.DEFAULT_PORT}"
    assert cli.os.environ["ANTHROPIC_AUTH_TOKEN"] == "tok-123"


def test_code_starts_daemon_when_not_running(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/claude")
    probe_calls = []

    def fake_probe(host, port, timeout=1.0):
        probe_calls.append(1)
        return len(probe_calls) > 1  # down on the first check, up from then on

    monkeypatch.setattr(cli, "_probe_health", fake_probe)
    spawn_calls = []
    monkeypatch.setattr(cli, "_spawn_background_daemon", lambda port: spawn_calls.append(port))
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port: "tok-456")
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    exec_calls = []
    _stub_launch(monkeypatch, exec_calls)

    assert cli.main(["code"]) == 0

    assert spawn_calls == [cli.DEFAULT_PORT]
    (binary, argv), = exec_calls
    assert os.path.basename(binary) == "claude"
    assert argv[0] == "claude"


def test_background_daemon_spawn_is_unbuffered_on_windows(monkeypatch, tmp_path):
    """Confirmed on real Windows hardware: stdout/stderr redirected to a file
    (as this spawn does) makes CPython fully block-buffer them. The daemon's
    startup banner and everything after it sat unflushed in
    daemon.out.log/.err.log indefinitely even though the daemon was up and
    serving requests. -u forces unbuffered I/O; only needed on Windows,
    where PYTHONUNBUFFERED isn't already set the way launchd/systemd units
    set it for the service-managed daemon."""
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))
    captured = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            captured["kw"] = kw

    monkeypatch.setattr(cli.subprocess, "Popen", FakePopen)

    cli._spawn_background_daemon(4317)

    argv = captured["argv"]
    if os.name == "nt":
        assert argv[0] == cli.sys.executable and argv[1] == "-u"
    else:
        assert "-u" not in argv


def test_code_gives_up_if_daemon_never_comes_up(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(cli, "_probe_health", lambda host, port, timeout=1.0: False)
    monkeypatch.setattr(cli, "_spawn_background_daemon", lambda port: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    assert cli.main(["code"]) == 1
    assert "didn't come up" in capsys.readouterr().err


def test_code_custom_port_is_forwarded_everywhere(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(cli, "_probe_health", lambda host, port, timeout=1.0: port == 5000)
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port: "tok")
    _stub_launch(monkeypatch)

    assert cli.main(["code", "--port", "5000"]) == 0
    assert cli.os.environ["ANTHROPIC_BASE_URL"] == f"http://{cli.LOOPBACK_HOST}:5000"


def test_code_status_line_args_receives_exactly_the_users_args_when_unconfigured(monkeypatch):
    """No configured launcher (config.launch_argv("claude") == ["claude"]) must
    produce argv byte-identical to the pre-ticket formula
    ["claude", *_status_line_args(port, claude_args), *claude_args]: the
    combined list fed to _status_line_args is exactly claude_args, nothing
    merged in ahead of it."""
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(cli, "launch_argv", lambda kind: ["claude"])
    monkeypatch.setattr(cli, "_probe_health", lambda host, port, timeout=1.0: True)
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port: "tok-abc")
    captured_combined = []

    def fake_status_line_args(port, combined):
        captured_combined.append(list(combined))
        return ["--settings", '{"fake":true}']

    monkeypatch.setattr(cli, "_status_line_args", fake_status_line_args)
    exec_calls = []
    monkeypatch.setattr(cli.os, "execvp", lambda cmd, args: exec_calls.append((cmd, args)))

    assert cli.main(["code", "--model", "opus"]) == 0

    assert captured_combined == [["--model", "opus"]]  # no configured extra args merged in
    (binary, argv), = exec_calls
    assert argv == ["claude", "--settings", '{"fake":true}', "--model", "opus"]
    assert binary == cli._resolve_launcher("claude")


def test_code_merges_configured_extra_args_ahead_of_the_users_own(monkeypatch):
    """A configured launcher's extra args (e.g. --dangerously-skip-permissions)
    go into `combined` before the user's own, and `combined` — not just the
    user's args — is what _status_line_args inspects (plan §3.3)."""
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/my-claude")
    monkeypatch.setattr(cli, "launch_argv", lambda kind: ["my-claude", "--dangerously-skip-permissions"])
    monkeypatch.setattr(cli, "_probe_health", lambda host, port, timeout=1.0: True)
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port: "tok-abc")
    captured_combined = []

    def fake_status_line_args(port, combined):
        captured_combined.append(list(combined))
        return []  # simulate the configured extra args already containing --settings

    monkeypatch.setattr(cli, "_status_line_args", fake_status_line_args)
    exec_calls = []
    monkeypatch.setattr(cli.os, "execvp", lambda cmd, args: exec_calls.append((cmd, args)))

    assert cli.main(["code", "--model", "opus"]) == 0

    assert captured_combined == [["--dangerously-skip-permissions", "--model", "opus"]]
    (binary, argv), = exec_calls
    assert argv == ["my-claude", "--dangerously-skip-permissions", "--model", "opus"]
    assert binary == cli._resolve_launcher("my-claude")


def test_code_which_check_uses_the_configured_executable_only(monkeypatch):
    """The shutil.which existence check must probe the configured executable
    alone — never a string containing the extra args too (plan §3.3)."""
    monkeypatch.setattr(cli, "launch_argv", lambda kind: ["my-claude", "--dangerously-skip-permissions"])
    which_calls = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: which_calls.append(name) or None)

    assert cli.main(["code"]) == 1
    assert which_calls == ["my-claude"]


def test_add_account_which_check_and_login_use_configured_executable_only(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "CLAUDE_ACCOUNTS_DIR", tmp_path / "claude-accounts")
    monkeypatch.setattr(cli, "launch_argv", lambda kind: ["my-claude", "--dangerously-skip-permissions"])
    which_calls = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: which_calls.append(name) or "/usr/bin/my-claude")
    run_calls = []

    def fake_run_tool(argv, **kw):
        run_calls.append(argv)
        return cli.subprocess.CompletedProcess(argv, 1)  # bail right after capturing

    monkeypatch.setattr(cli, "_run_tool", fake_run_tool)

    assert cli.add_account() == 1
    assert which_calls == ["my-claude"]
    assert run_calls == [["my-claude", "auth", "login"]]  # never the extra --dangerously-skip-permissions


def test_add_codex_account_which_check_and_login_use_configured_executable_only(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "CODEX_ACCOUNTS_DIR", tmp_path / "codex-accounts")
    monkeypatch.setattr(cli, "launch_argv", lambda kind: ["my-codex", "--some-flag"])
    which_calls = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: which_calls.append(name) or "/usr/bin/my-codex")
    run_calls = []

    def fake_run_tool(argv, **kw):
        run_calls.append(argv)
        return cli.subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(cli, "_run_tool", fake_run_tool)

    assert cli.add_codex_account() == 1
    assert which_calls == ["my-codex"]
    assert run_calls == [["my-codex", "login"]]  # never the extra --some-flag


def test_reauth_which_check_and_login_use_configured_executable_only(monkeypatch, tmp_path):
    from claude_unlimited.config import Pool, Profile

    monkeypatch.setattr(cli, "CLAUDE_ACCOUNTS_DIR", tmp_path / "claude-accounts")
    monkeypatch.setattr(cli, "launch_argv", lambda kind: ["my-claude", "--dangerously-skip-permissions"])
    which_calls = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: which_calls.append(name) or "/usr/bin/my-claude")
    monkeypatch.setattr(cli, "load_pool", lambda: Pool(profiles=[Profile(id="a", name="A", kind="oauth")]))
    monkeypatch.setattr(cli, "_fetch_live_profiles", lambda host, port: None)
    run_calls = []

    def fake_run_tool(argv, **kw):
        run_calls.append(argv)
        return cli.subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(cli, "_run_tool", fake_run_tool)

    assert cli.reauth(cli.DEFAULT_PORT) == 1
    assert which_calls == ["my-claude"]
    assert run_calls == [["my-claude", "auth", "login"]]


def test_install_port_precedence_env_var_used_when_no_explicit_flag(monkeypatch):
    """Confirms install's --port reader goes through resolve_port(): with no
    --port flag, CLAUDE_UNLIMITED_PORT wins over DEFAULT_PORT (plan §3.4)."""
    calls = []
    monkeypatch.setattr(daemon_installer, "install", lambda port: calls.append(port))
    _serving(monkeypatch, None, __version__)
    monkeypatch.setenv("CLAUDE_UNLIMITED_PORT", "6001")

    assert cli.main(["install"]) == 0
    assert calls == [6001]


def test_status_not_installed(monkeypatch, capsys):
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": False, "running": False, "pid": None})
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Not installed" in out


def test_status_installed_and_running(monkeypatch, capsys):
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": True, "running": True, "pid": 999})
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "999" in out
    assert "running" in out.lower()


def test_status_installed_not_running(monkeypatch, capsys):
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": True, "running": False, "pid": None})
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "not currently running" in out.lower()


def test_install_success(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(daemon_installer, "install", lambda port: calls.append(port))
    _serving(monkeypatch, None, __version__)
    assert cli.main(["install", "--port", "5000"]) == 0
    assert calls == [5000]
    assert "Installed" in capsys.readouterr().out


def test_install_default_port(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(daemon_installer, "install", lambda port: calls.append(port))
    _serving(monkeypatch, None, __version__)
    assert cli.main(["install"]) == 0
    assert calls == [cli.DEFAULT_PORT]


def test_install_failure_returns_nonzero(monkeypatch, capsys):
    def boom(port):
        raise daemon_installer.DaemonInstallerError("launchctl exploded")

    monkeypatch.setattr(daemon_installer, "install", boom)
    _serving(monkeypatch, None)
    assert cli.main(["install"]) == 1
    assert "launchctl exploded" in capsys.readouterr().err


def test_uninstall_success(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(daemon_installer, "uninstall", lambda: calls.append(True))
    assert cli.main(["uninstall"]) == 0
    assert calls == [True]
    assert "Uninstalled" in capsys.readouterr().out


def test_uninstall_failure_returns_nonzero(monkeypatch, capsys):
    def boom():
        raise daemon_installer.DaemonInstallerError("nope")

    monkeypatch.setattr(daemon_installer, "uninstall", boom)
    assert cli.main(["uninstall"]) == 1


def test_service_start_success(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(daemon_installer, "start", lambda: calls.append(True))
    assert cli.main(["service-start"]) == 0
    assert calls == [True]
    assert "Started" in capsys.readouterr().out


def test_service_start_failure(monkeypatch, capsys):
    def boom():
        raise daemon_installer.DaemonInstallerError("not installed")

    monkeypatch.setattr(daemon_installer, "start", boom)
    assert cli.main(["service-start"]) == 1


def test_service_stop_success(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(daemon_installer, "stop", lambda: calls.append(True))
    assert cli.main(["service-stop"]) == 0
    assert calls == [True]
    assert "Stopped" in capsys.readouterr().out


def test_service_stop_failure(monkeypatch, capsys):
    def boom():
        raise daemon_installer.DaemonInstallerError("not installed")

    monkeypatch.setattr(daemon_installer, "stop", boom)
    assert cli.main(["service-stop"]) == 1


def test_purge_refuses_without_confirmation_when_not_interactive(monkeypatch, capsys):
    """Deleting credentials and config is irreversible, so a piped invocation
    must not proceed silently."""
    from claude_unlimited import cli

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert cli.purge(assume_yes=False) == 1
    assert "Refusing to purge" in capsys.readouterr().err


def test_purge_deletes_credentials_before_removing_the_config(monkeypatch, tmp_path):
    """The config is the only record of which Profiles exist. Removing it
    first would strand their credentials in the keystore with no way left to
    enumerate them."""
    from claude_unlimited import cli
    from claude_unlimited.config import Pool, Profile

    order = []
    deleted = []

    monkeypatch.setattr(cli, "load_pool", lambda: Pool(profiles=[
        Profile(id="p1", name="A", kind="oauth", priority=1, automatic=True, enabled=True, account_uuid="u"),
        Profile(id="p2", name="B", kind="api", priority=2, automatic=True, enabled=True),
    ]))
    import claude_unlimited.secret_store as store
    monkeypatch.setattr(store, "delete_token", lambda pid: (order.append("delete_token"), deleted.append(pid)))

    real_rmtree = cli.shutil.rmtree
    monkeypatch.setattr(cli.shutil, "rmtree",
                        lambda p, **k: order.append("rmtree"))
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".claude-unlimited").mkdir()
    monkeypatch.setattr(cli.daemon_installer, "stop", lambda: None)
    monkeypatch.setattr(cli.daemon_installer, "uninstall", lambda: None)

    assert cli.purge(assume_yes=True) == 0
    assert deleted == ["p1", "p2"]
    assert order.index("delete_token") < order.index("rmtree")


def test_purge_never_touches_the_users_claude_directory(monkeypatch, tmp_path):
    from claude_unlimited import cli
    from claude_unlimited.config import Pool

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "CLAUDE.md").write_text("mine")
    (tmp_path / ".claude-unlimited").mkdir()

    monkeypatch.setattr(cli, "load_pool", lambda: Pool(profiles=[]))
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(cli.daemon_installer, "stop", lambda: None)
    monkeypatch.setattr(cli.daemon_installer, "uninstall", lambda: None)

    assert cli.purge(assume_yes=True) == 0
    assert (claude_dir / "CLAUDE.md").read_text() == "mine"
    assert not (tmp_path / ".claude-unlimited").exists()


def test_purge_stops_a_daemon_the_service_manager_does_not_own(monkeypatch, tmp_path):
    """install.sh starts the daemon detached, so it is not a launchd job.
    Deregistering the service leaves it serving the dashboard."""
    from claude_unlimited import cli

    (tmp_path / ".claude-unlimited").mkdir()
    (tmp_path / ".claude-unlimited" / "daemon.pid").write_text("4242")
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))

    killed = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    # Port stops answering after the signal.
    monkeypatch.setattr(cli, "_probe_health", lambda *a, **k: False)

    cli._stop_running_daemon(4317)
    assert killed and killed[0][0] == 4242


def test_purge_warns_when_something_still_holds_the_port(monkeypatch, tmp_path, capsys):
    """Removing the files while a daemon is still serving would leave it
    running against deleted code — say so rather than reporting success."""
    from claude_unlimited import cli

    (tmp_path / ".claude-unlimited").mkdir()
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(cli, "_probe_health", lambda *a, **k: True)   # never stops
    # Without these two, "still holds the port" fell through to a REAL lsof on
    # 4317 and SIGTERMed the developer's running daemon on every suite run.
    monkeypatch.setattr(cli, "_pids_listening_on", lambda port: [])
    killed = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append(pid))

    cli._stop_running_daemon(4317)
    assert "still serving" in capsys.readouterr().out
    assert killed == []


def test_purge_deregisters_the_service_before_removing_files(monkeypatch, tmp_path):
    """Order matters: if the login service is still registered when the app
    directory goes, it points at missing code on every future login."""
    from claude_unlimited import cli
    from claude_unlimited.config import Pool

    order = []
    monkeypatch.setattr(cli, "load_pool", lambda: Pool(profiles=[]))
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".claude-unlimited").mkdir()
    monkeypatch.setattr(cli.daemon_installer, "uninstall", lambda: order.append("deregister"))
    monkeypatch.setattr(cli.daemon_installer, "stop", lambda: None)
    monkeypatch.setattr(cli, "_stop_running_daemon", lambda *a, **k: order.append("stop_daemon"))
    monkeypatch.setattr(cli.shutil, "rmtree", lambda p, **k: order.append("rmtree"))

    assert cli.purge(assume_yes=True) == 0
    assert order.index("deregister") < order.index("rmtree")
    assert order.index("stop_daemon") < order.index("rmtree")


def test_purge_never_removes_the_users_own_claude_login(monkeypatch, tmp_path):
    """The isolated logins add-account creates are ours to clean up. The
    un-suffixed 'Claude Code-credentials' is the user's real login and must
    survive — losing it would log them out of plain `claude`."""
    from claude_unlimited import cli
    from claude_unlimited.anthropic_oauth import MACOS_KEYCHAIN_SERVICE

    from claude_unlimited import anthropic_oauth
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    # The removal decides by platform.system(), not sys.platform — without
    # this the test only passes on a Mac, and fails on Linux CI.
    monkeypatch.setattr(anthropic_oauth.platform, "system", lambda: "Darwin")
    deleted = []

    class _R:
        returncode = 0

    def fake_run(cmd, **kw):
        deleted.append(cmd[cmd.index("-s") + 1])
        return _R()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    cli._remove_isolated_claude_logins([
        tmp_path / "claude-accounts" / "aaaa1111",
        tmp_path / "claude-accounts" / "bbbb2222",
    ])

    assert len(deleted) == 2
    assert MACOS_KEYCHAIN_SERVICE not in deleted, "must never delete the real login"
    for service in deleted:
        assert service.startswith(MACOS_KEYCHAIN_SERVICE + "-"), service


def test_purge_leaves_the_claude_directory_and_its_credentials_alone(monkeypatch, tmp_path):
    """~/.claude holds the user's own Claude Code setup and login."""
    from claude_unlimited import cli
    from claude_unlimited.config import Pool

    claude = tmp_path / ".claude"
    (claude / "projects").mkdir(parents=True)
    (claude / ".credentials.json").write_text("the user's own login")
    (tmp_path / ".claude-unlimited").mkdir()

    monkeypatch.setattr(cli, "load_pool", lambda: Pool(profiles=[]))
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(cli.daemon_installer, "uninstall", lambda: None)
    monkeypatch.setattr(cli.daemon_installer, "stop", lambda: None)
    monkeypatch.setattr(cli, "_stop_running_daemon", lambda *a, **k: None)

    assert cli.purge(assume_yes=True) == 0
    assert (claude / ".credentials.json").read_text() == "the user's own login"
    assert (claude / "projects").exists()


def test_install_stops_a_daemon_already_holding_the_port(monkeypatch, capsys):
    # Registering the service does not touch a detached daemon, so without an
    # explicit stop the old process keeps the port and the new one never binds.
    stopped = []
    monkeypatch.setattr(daemon_installer, "install", lambda port: None)
    monkeypatch.setattr(daemon_installer, "stop", lambda: stopped.append("service"))
    monkeypatch.setattr(cli, "_stop_running_daemon", lambda port, **kw: stopped.append(port))
    _serving(monkeypatch, "0.0.1-old", __version__)

    assert cli.main(["install", "--port", "4317"]) == 0
    assert "service" in stopped and 4317 in stopped
    assert "0.0.1-old" in capsys.readouterr().out


def test_install_reports_failure_when_an_old_version_still_serves(monkeypatch, capsys):
    # The exact silent failure: files installed, health check passes, but the
    # version answering is not the one just installed.
    monkeypatch.setattr(daemon_installer, "install", lambda port: None)
    monkeypatch.setattr(daemon_installer, "stop", lambda: None)
    monkeypatch.setattr(cli, "_stop_running_daemon", lambda port, **kw: None)
    monkeypatch.setattr(cli, "_wait_for_version",
                        lambda host, port, expected, timeout=20.0: "0.0.1-old")
    _serving(monkeypatch, "0.0.1-old")

    assert cli.main(["install"]) == 1
    err = capsys.readouterr().err
    assert "0.0.1-old" in err and "still served" in err


def test_install_reports_failure_when_nothing_comes_up(monkeypatch, capsys):
    monkeypatch.setattr(daemon_installer, "install", lambda port: None)
    monkeypatch.setattr(cli, "_wait_for_version",
                        lambda host, port, expected, timeout=20.0: None)
    _serving(monkeypatch, None)
    assert cli.main(["install"]) == 1
    assert "nothing is answering" in capsys.readouterr().err


def test_stop_kills_a_stray_daemon_the_pid_file_does_not_know_about(monkeypatch, capsys):
    # The real-world shape: a detached daemon under a different interpreter
    # holds the port, and the pid file names a different (already dead) one.
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: cli.Path("/nonexistent")))
    monkeypatch.setattr(cli, "_pids_listening_on", lambda port: [4242])
    monkeypatch.setattr(cli, "_is_our_daemon", lambda pid: True)

    killed = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    # Serving until the kill lands, then gone.
    health = iter([True, True, False, False, False, False, False, False])
    monkeypatch.setattr(cli, "_probe_health",
                        lambda host, port, timeout=1.0: next(health, False))

    cli._stop_running_daemon(4317)
    assert killed and killed[0][0] == 4242
    assert "pid 4242" in capsys.readouterr().out


def test_stop_never_kills_a_process_that_is_not_ours(monkeypatch, capsys):
    # Holding the port is not licence to kill it — that could be anything.
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: cli.Path("/nonexistent")))
    monkeypatch.setattr(cli, "_pids_listening_on", lambda port: [4242])
    monkeypatch.setattr(cli, "_is_our_daemon", lambda pid: False)

    killed = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(cli, "_probe_health", lambda host, port, timeout=1.0: True)

    cli._stop_running_daemon(4317)
    assert killed == []
    assert "WARNING" in capsys.readouterr().out


def test_is_our_daemon_matches_on_the_command_line(monkeypatch):
    class R:
        stdout = ("/opt/homebrew/Cellar/python@3.14/.../Python "
                  "-m claude_unlimited start --port 4317")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: R())
    assert cli._is_our_daemon(1) is True

    class Other:
        stdout = "/usr/bin/some-other-server --port 4317"
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: Other())
    assert cli._is_our_daemon(1) is False


# ---------------------------------------------------------------------------
# A bad --port is a typo, not a crash. config.resolve_port() raises ValueError
# on an out-of-range explicit port on purpose, but main() calls it inside the
# dispatch expression, so without _resolve_port_or_exit() the raise reaches
# the terminal as a stack trace. argparse exits 2 with one line for a bad
# flag; a bad flag VALUE has to look the same.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["0", "80", "1023", "65536", "99999"])
def test_bad_port_flag_exits_cleanly_instead_of_tracebacking(bad, monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_UNLIMITED_PORT", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["start", "--port", bad])

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("claude-unlimited: ")
    assert bad in err
    assert "Traceback" not in err
    assert err.count("\n") == 1, "a bad flag value gets one line, like argparse"


def test_every_port_taking_subcommand_uses_the_guarded_resolver():
    """main() must never call resolve_port() bare — one unguarded call site is
    one subcommand that still tracebacks on a typo."""
    import inspect

    source = inspect.getsource(cli.main)
    assert "resolve_port(args.port)" not in source
    assert source.count("_resolve_port_or_exit(args.port)") == 7


def test_a_good_port_flag_still_resolves():
    assert cli._resolve_port_or_exit(4400) == 4400
