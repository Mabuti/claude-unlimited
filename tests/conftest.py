import pytest

import claude_unlimited.config as config
import claude_unlimited.notifications as notifications


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
    """
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    # config.resolve_port() honours CLAUDE_UNLIMITED_PORT BEFORE it ever
    # falls through to settings.port, so redirecting APP_DIR/CONFIG_FILE
    # above is not enough to isolate port resolution: if this var is set in
    # the developer's or CI's shell, it wins over the isolated config and
    # reintroduces the exact non-hermetic, machine-dependent failure this
    # fixture exists to prevent. A test that specifically wants to exercise
    # the env-var precedence can still set it itself after this fixture runs.
    monkeypatch.delenv("CLAUDE_UNLIMITED_PORT", raising=False)
