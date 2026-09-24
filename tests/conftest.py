import pytest

import claude_unlimited.activity as activity
import claude_unlimited.config as config
import claude_unlimited.daemon as daemon
import claude_unlimited.hud as hud
import claude_unlimited.notifications as notifications
import claude_unlimited.placeholder_token as placeholder_token
import claude_unlimited.project_usage as project_usage
import claude_unlimited.runtime_state as runtime_state
import claude_unlimited.session_tokens as session_tokens
import claude_unlimited.usage_history as usage_history
import claude_unlimited.usage_probe as usage_probe


@pytest.fixture(autouse=True)
def no_real_desktop_notifications(monkeypatch):
    """The test suite must never pop a real desktop notification.

    All three OS-specific senders are stubbed, because notify_if_enabled
    calls all three and each platform's own guard only helps off-platform.
    Tests that care about what would have fired can monkeypatch any of
    these again with their own recorder.
    """
    monkeypatch.setattr(notifications, "send_macos_notification", lambda title, message: None)
    monkeypatch.setattr(notifications, "send_linux_notification", lambda title, message: None)
    monkeypatch.setattr(notifications, "send_windows_notification", lambda title, message: None)


@pytest.fixture(autouse=True)
def isolated_app_dir(monkeypatch, tmp_path):
    """No test may ever read or write the developer's real
    ~/.claude-unlimited/config.json.

    Any code path that reaches config.load_pool() — including
    config.resolve_port(), which falls through to settings.port — picks up
    whatever is on THIS machine right now unless APP_DIR/CONFIG_FILE are
    redirected first. A test-specific fixture (e.g. running_server) is free
    to point them at its own tmp_path afterward; monkeypatch.setattr just
    overwrites this one, and since both draw from the same per-test
    tmp_path fixture, the two never disagree.

    Redirecting config.APP_DIR/CONFIG_FILE is not enough on its own: several
    other modules bind their own file path from APP_DIR at IMPORT time
    (`FOO_FILE = APP_DIR / "foo.json"`), so patching config.APP_DIR after
    import never reaches them — they keep pointing at the real
    ~/.claude-unlimited/ for the rest of the process. On a developer machine
    that silently reads/writes the real file instead of failing loudly; on a
    clean CI runner (no ~/.claude-unlimited/ at all) it raises
    FileNotFoundError instead. Each of those constants is redirected here too,
    to the same tmp_path, for the same reason CONFIG_FILE is.

    claude_unlimited.db (the v1.3.1 SQLite store) needs no constant of its
    own here: its module docstring guarantees it resolves its path from
    config.APP_DIR AT CALL TIME, never at import, specifically so the test
    suite's per-test APP_DIR swap is enough to isolate it. Redirecting
    config.APP_DIR above already covers it.
    """
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(activity, "ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(daemon, "PID_FILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(placeholder_token, "TOKEN_FILE", tmp_path / "placeholder_token")
    monkeypatch.setattr(project_usage, "USAGE_FILE", tmp_path / "project_usage.json")
    monkeypatch.setattr(runtime_state, "RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr(session_tokens, "SESSION_TOKENS_FILE", tmp_path / "session_tokens.json")
    monkeypatch.setattr(usage_history, "USAGE_HISTORY_FILE", tmp_path / "usage_history.jsonl")
    # config.resolve_port() honours CLAUDE_UNLIMITED_PORT BEFORE it ever
    # falls through to settings.port, so redirecting APP_DIR/CONFIG_FILE
    # above is not enough to isolate port resolution: if this var is set in
    # the developer's or CI's shell, it wins over the isolated config and
    # reintroduces the exact non-hermetic, machine-dependent failure this
    # fixture exists to prevent. A test that specifically wants to exercise
    # the env-var precedence can still set it itself after this fixture runs.
    monkeypatch.delenv("CLAUDE_UNLIMITED_PORT", raising=False)

    # The HUD installer writes into ~/Applications and ~/Library/LaunchAgents.
    # A test that reached those would uninstall the user's own HUD — so point
    # every one of its paths at tmp_path here, not only in the tests that
    # exercise it.
    monkeypatch.setattr(hud, "BUNDLE_DIR", tmp_path / "Applications")
    monkeypatch.setattr(hud, "LEGACY_BUNDLE", tmp_path / "Applications" / "CapacityWidget.app")
    monkeypatch.setattr(hud, "LAUNCH_AGENT", tmp_path / "LaunchAgents" / f"{hud.LABEL}.plist")
    monkeypatch.setattr(hud, "STAMP", tmp_path / "hud-version")
    monkeypatch.setattr(hud, "OPT_OUT", tmp_path / "hud-removed")
    monkeypatch.setattr(hud, "APP_DIR", tmp_path)


@pytest.fixture(autouse=True)
def no_real_usage_endpoint_calls(monkeypatch):
    """usage_probe's single network seam refuses by default; a test that
    exercises the HTTP layer installs its own fake."""
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to reach a real usage endpoint")
    monkeypatch.setattr(usage_probe, "_urlopen", refuse)


@pytest.fixture(autouse=True)
def no_background_threads_outliving_a_test(monkeypatch):
    """A thread started inside a test must not outlive it.

    Gateway schedules credential checks on a background thread. When a test
    ended before that thread did, monkeypatch had already restored the real
    config path — and the thread wrote its test pool over the developer's four
    live Profiles. Inside the suite the checks run inline instead, on the
    caller's thread, where the redirected paths still apply.
    """
    import claude_unlimited.gateway as gateway

    def run_inline(self, pool):
        try:
            self.run_credential_checks_now(pool)
        except Exception:
            pass

    monkeypatch.setattr(gateway.Gateway, "_schedule_credential_checks", run_inline)


@pytest.fixture(autouse=True)
def no_real_process_signals(monkeypatch):
    """The suite must never find or signal a real process.

    cli._stop_running_daemon() falls back to whoever holds the port, via a
    real `lsof`. One test reached that fallback unmocked, found the
    developer's live daemon on 4317, and SIGTERMed it on every run (launchd
    quietly restarted it). The discovery seam now finds nothing by default;
    a test that needs a stray pid installs its own. The pid-file route is
    closed by no_real_home below."""
    import claude_unlimited.cli as cli
    monkeypatch.setattr(cli, "_pids_listening_on", lambda port: [])


@pytest.fixture(autouse=True)
def no_real_home(monkeypatch, tmp_path):
    """Path.home() must never be the developer's home during a test.

    cli._stop_running_daemon() reads ~/.claude-unlimited/daemon.pid and
    signals that pid; a test reaching it without patching Path.home would
    SIGTERM the live daemon (the lsof fallback above was one such route).
    Path.home() follows $HOME, so pointing $HOME at an empty directory closes
    every route at once. Tests that patch cli.Path.home still win."""
    home = tmp_path / "_isolated_home"   # not "home": tests create their own tmp_path/"home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
