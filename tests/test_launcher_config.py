"""Tests for the configurable-launch-command + configurable-port config layer
(docs/tickets/configurable-launchers-and-port.plan.md, ticket W1).

Covers: the LAUNCHER_KINDS registry, launch_argv()'s splitting/fallback
behaviour on POSIX and Windows, resolve_port()'s precedence chain, and
validated_settings_changes()'s handling of `launchers` (validated) and
`port` (rejected, pointing at POST /api/settings/port)."""
import pytest

from claude_unlimited import config


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")


# ---------------------------------------------------------------------------
# LAUNCHER_KINDS registry (plan §3.1)
# ---------------------------------------------------------------------------

def test_launcher_kinds_registry_has_claude_and_codex():
    by_name = {lk.kind: lk for lk in config.LAUNCHER_KINDS}
    assert set(by_name) == {"claude", "codex"}
    assert by_name["claude"].default_command == "claude"
    assert by_name["claude"].label_key == "settings.launchers.claude"
    assert by_name["claude"].used_by == "claude-unlimited code"
    assert by_name["codex"].default_command == "codex"
    assert by_name["codex"].label_key == "settings.launchers.codex"


# ---------------------------------------------------------------------------
# launch_argv(): fallback to registry default
# ---------------------------------------------------------------------------

def test_launch_argv_falls_back_to_registry_default_when_unconfigured(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    assert config.launch_argv("claude") == ["claude"]
    assert config.launch_argv("codex") == ["codex"]


def test_launch_argv_falls_back_when_configured_value_is_blank(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    pool = config.Pool(settings=config.Settings(launchers={"claude": "   "}))
    config.save_pool(pool)
    assert config.launch_argv("claude") == ["claude"]


def test_launch_argv_falls_back_for_a_kind_not_in_the_registry(tmp_path, monkeypatch):
    # Not reachable through the validated API (unknown kinds are rejected),
    # but launch_argv() itself must degrade gracefully rather than KeyError,
    # since it's also called for whatever kind a future caller names.
    _isolate(monkeypatch, tmp_path)
    assert config.launch_argv("some-future-cli") == ["some-future-cli"]


# ---------------------------------------------------------------------------
# launch_argv(): POSIX splitting
# ---------------------------------------------------------------------------

def test_launch_argv_posix_splits_with_shlex(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(config.os, "name", "posix")
    pool = config.Pool(settings=config.Settings(
        launchers={"claude": "claude --dangerously-skip-permissions --model 'my model'"}))
    config.save_pool(pool)
    assert config.launch_argv("claude") == [
        "claude", "--dangerously-skip-permissions", "--model", "my model",
    ]


# ---------------------------------------------------------------------------
# launch_argv(): Windows splitting — backslash paths and quoted paths with
# spaces (plan §3.2 explicitly requires both).
# ---------------------------------------------------------------------------

def _stub_pool_with_launchers(monkeypatch, launchers):
    # launch_argv() reads the configured value via load_pool(). Stubbing
    # load_pool() directly (rather than round-tripping through a real
    # config.json) avoids load_pool()'s own unrelated `Path.home()` call,
    # which raises under a monkeypatched os.name=="nt" on a POSIX test
    # runner (Python 3.13's pathlib refuses to build a WindowsPath here) —
    # a pre-existing quirk of load_pool(), out of this ticket's write set.
    monkeypatch.setattr(config, "load_pool",
                         lambda: config.Pool(settings=config.Settings(launchers=launchers)))


def test_launch_argv_windows_absolute_backslash_path(monkeypatch):
    _stub_pool_with_launchers(monkeypatch, {"claude": r"C:\Users\x\claude.cmd --verbose"})
    monkeypatch.setattr(config.os, "name", "nt")
    # posix=True would mangle the backslashes (eating them); the path must
    # survive intact.
    assert config.launch_argv("claude") == [r"C:\Users\x\claude.cmd", "--verbose"]


def test_launch_argv_windows_quoted_path_with_space(monkeypatch):
    _stub_pool_with_launchers(monkeypatch, {"claude": r'"C:\Program Files\claude\claude.cmd" --verbose'})
    monkeypatch.setattr(config.os, "name", "nt")
    # The surrounding quote pair must be stripped from the token, and the
    # space inside must not split the path into two argv entries.
    assert config.launch_argv("claude") == [
        r"C:\Program Files\claude\claude.cmd", "--verbose",
    ]


def test_launch_argv_windows_does_not_strip_unmatched_or_internal_quotes(monkeypatch):
    _stub_pool_with_launchers(monkeypatch, {"claude": r'C:\bin\claude.cmd --label "release"'})
    monkeypatch.setattr(config.os, "name", "nt")
    # Only a token whose *surrounding* pair matches gets stripped; the exe
    # token here carries no quotes at all and must pass through untouched.
    assert config.launch_argv("claude")[0] == r"C:\bin\claude.cmd"


# ---------------------------------------------------------------------------
# Unbalanced quotes raise ValueError (both platforms)
# ---------------------------------------------------------------------------

def test_split_command_unbalanced_quote_raises_value_error_posix(monkeypatch):
    monkeypatch.setattr(config.os, "name", "posix")
    with pytest.raises(ValueError):
        config._split_command('claude --model "unterminated')


def test_split_command_unbalanced_quote_raises_value_error_windows(monkeypatch):
    monkeypatch.setattr(config.os, "name", "nt")
    with pytest.raises(ValueError):
        config._split_command('claude --model "unterminated')


# ---------------------------------------------------------------------------
# resolve_port(): explicit > env > settings.port > DEFAULT_PORT (plan §3.4)
# ---------------------------------------------------------------------------

def test_resolve_port_explicit_flag_wins_over_everything(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_UNLIMITED_PORT", "6000")
    config.save_pool(config.Pool(settings=config.Settings(port=7000)))
    assert config.resolve_port(9999) == 9999


def test_resolve_port_env_wins_over_settings_and_default(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_UNLIMITED_PORT", "6000")
    config.save_pool(config.Pool(settings=config.Settings(port=7000)))
    assert config.resolve_port(None) == 6000


def test_resolve_port_settings_wins_over_default(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.delenv("CLAUDE_UNLIMITED_PORT", raising=False)
    config.save_pool(config.Pool(settings=config.Settings(port=7000)))
    assert config.resolve_port(None) == 7000


def test_resolve_port_falls_back_to_default_port(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.delenv("CLAUDE_UNLIMITED_PORT", raising=False)
    # No config.json at all yet.
    assert config.resolve_port(None) == config._default_port()


def test_resolve_port_ignores_non_numeric_env(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_UNLIMITED_PORT", "not-a-number")
    config.save_pool(config.Pool(settings=config.Settings(port=7000)))
    assert config.resolve_port(None) == 7000


# ---------------------------------------------------------------------------
# resolve_port(): range validation. MIN_PORT/MAX_PORT are the SAME pair
# POST /api/settings/port enforces (daemon.py imports them), so a port can no
# longer sail through CLAUDE_UNLIMITED_PORT or --port that the Dashboard
# would reject out of hand.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["0", "80", "65536", "99999", "-1"])
def test_resolve_port_out_of_range_env_falls_back_to_settings(bad, tmp_path, monkeypatch):
    # Same treatment as a non-numeric value: the environment is inherited from
    # a service unit or a shell profile, not a place anyone reads errors, so a
    # bad value there must not stop a daemon that worked yesterday.
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_UNLIMITED_PORT", bad)
    config.save_pool(config.Pool(settings=config.Settings(port=7000)))
    assert config.resolve_port(None) == 7000


def test_resolve_port_out_of_range_env_falls_back_to_default_when_no_settings(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_UNLIMITED_PORT", "99999")
    assert config.resolve_port(None) == config._default_port()


@pytest.mark.parametrize("edge", [config.MIN_PORT, config.MAX_PORT, 5000])
def test_resolve_port_accepts_in_range_env(edge, tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_UNLIMITED_PORT", str(edge))
    config.save_pool(config.Pool(settings=config.Settings(port=7000)))
    assert config.resolve_port(None) == edge


@pytest.mark.parametrize("bad", [0, 80, 1023, 65536, 99999, -1])
def test_resolve_port_out_of_range_explicit_raises(bad, tmp_path, monkeypatch):
    # An explicit --port is the one value a human definitely typed, so it is
    # an error rather than a silent fallback onto a port they never asked for.
    _isolate(monkeypatch, tmp_path)
    monkeypatch.delenv("CLAUDE_UNLIMITED_PORT", raising=False)
    config.save_pool(config.Pool(settings=config.Settings(port=7000)))
    with pytest.raises(ValueError) as excinfo:
        config.resolve_port(bad)
    assert str(config.MIN_PORT) in str(excinfo.value)
    assert str(config.MAX_PORT) in str(excinfo.value)


@pytest.mark.parametrize("edge", [config.MIN_PORT, config.MAX_PORT, 9999])
def test_resolve_port_accepts_in_range_explicit(edge, tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_UNLIMITED_PORT", "6000")
    config.save_pool(config.Pool(settings=config.Settings(port=7000)))
    assert config.resolve_port(edge) == edge


def test_port_in_range_boundaries():
    assert not config.port_in_range(config.MIN_PORT - 1)
    assert config.port_in_range(config.MIN_PORT)
    assert config.port_in_range(config.MAX_PORT)
    assert not config.port_in_range(config.MAX_PORT + 1)


# ---------------------------------------------------------------------------
# validated_settings_changes(): launchers accepted/validated, port rejected
# ---------------------------------------------------------------------------

def test_validated_settings_changes_accepts_valid_launchers():
    changes = config.validated_settings_changes({
        "launchers": {"claude": "claude --dangerously-skip-permissions", "codex": ""},
    })
    assert changes["launchers"] == {
        "claude": "claude --dangerously-skip-permissions", "codex": "",
    }


def test_validated_settings_changes_rejects_unknown_launcher_kind():
    with pytest.raises(ValueError, match="unknown CLI kind"):
        config.validated_settings_changes({"launchers": {"not-a-real-cli": "foo"}})


def test_validated_settings_changes_rejects_non_string_launcher_value():
    with pytest.raises(ValueError, match="must be a string"):
        config.validated_settings_changes({"launchers": {"claude": 123}})


def test_validated_settings_changes_rejects_unparseable_launcher_command():
    with pytest.raises(ValueError):
        config.validated_settings_changes({"launchers": {"claude": 'claude --model "unterminated'}})


def test_validated_settings_changes_rejects_port_naming_the_dedicated_endpoint():
    with pytest.raises(ValueError, match="POST /api/settings/port"):
        config.validated_settings_changes({"port": 5000})


def test_validated_settings_changes_rejects_port_even_alongside_other_valid_fields():
    # A bundle import carrying `port` (every export now does, via asdict())
    # must not silently drop it and apply the rest — it must fail loudly.
    with pytest.raises(ValueError, match="POST /api/settings/port"):
        config.validated_settings_changes({"update_mode": "manual", "port": 5000})


def test_resolve_port_falls_back_when_settings_port_is_out_of_range(tmp_path, monkeypatch):
    """settings.port has one validating writer (POST /api/settings/port), but
    config.json is a plain file run_foreground's bind-failure message invites
    the user to hand-edit — so an out-of-range value there is reachable, and
    must fall back rather than be handed to bind()."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.delenv("CLAUDE_UNLIMITED_PORT", raising=False)
    config.save_pool(config.Pool(settings=config.Settings(port=99999)))
    assert config.resolve_port(None) == config._default_port()
