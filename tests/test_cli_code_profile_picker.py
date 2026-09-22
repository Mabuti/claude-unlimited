import os
import pytest
from datetime import datetime, timedelta, timezone

import claude_unlimited.cli as cli
from claude_unlimited.config import Profile


def _profiles():
    return [
        Profile(id="id-a", name="Alice", kind="oauth"),
        Profile(id="id-b", name="Bob", kind="oauth"),
        Profile(id="id-c", name="API LLM", kind="api"),
    ]


# ---- _match_profile ----

def test_match_profile_by_exact_id():
    assert cli._match_profile(_profiles(), "id-b").name == "Bob"


def test_match_profile_by_exact_name_case_insensitive():
    assert cli._match_profile(_profiles(), "bob").id == "id-b"


def test_match_profile_by_unique_substring():
    assert cli._match_profile(_profiles(), "llm").id == "id-c"


def test_match_profile_no_match_returns_none():
    assert cli._match_profile(_profiles(), "nonexistent") is None


def test_match_profile_ambiguous_substring_returns_none():
    profiles = [Profile(id="1", name="Work A", kind="oauth"), Profile(id="2", name="Work B", kind="oauth")]
    assert cli._match_profile(profiles, "work") is None


# ---- _prompt_profile_choice ----

def test_prompt_default_empty_input_means_rotated_accounts(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert cli._prompt_profile_choice(_profiles()) is None


def test_prompt_explicit_1_means_rotated_accounts(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    assert cli._prompt_profile_choice(_profiles()) is None


def test_prompt_2_picks_the_first_profile(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    assert cli._prompt_profile_choice(_profiles()).name == "Alice"


def test_prompt_last_option_picks_the_last_profile(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "4")
    assert cli._prompt_profile_choice(_profiles()).name == "API LLM"


def test_prompt_out_of_range_falls_back_to_rotated_accounts(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "99")
    assert cli._prompt_profile_choice(_profiles()) is None
    assert "Rotated accounts" in capsys.readouterr().out


def test_prompt_non_numeric_falls_back_to_rotated_accounts(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "banana")
    assert cli._prompt_profile_choice(_profiles()) is None
    assert "Rotated accounts" in capsys.readouterr().out


# ---- code() wiring ----

@pytest.fixture
def code_env(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(cli, "_probe_health", lambda host, port, timeout=1.0: True)
    # code() makes two best-effort daemon calls we must never let touch a live
    # daemon on 4317 in a test: POST /api/models/refresh, and GET /v1/models
    # (for the /model picker labels). Stub both.
    monkeypatch.setattr(cli, "_request_models_refresh", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_fetch_parity_labels", lambda *a, **kw: {})
    # code() self-heals the CLI launchers; never let that touch the real
    # ~/.local/bin in a test.
    monkeypatch.setattr(cli.updater, "ensure_cli_aliases", lambda *a, **kw: None)
    execs = []
    monkeypatch.setattr(cli.os, "execvp", lambda file, args: execs.append((file, args)))
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    return execs


def test_code_with_profile_flag_fetches_a_session_token_not_the_placeholder_token(monkeypatch, code_env):
    from claude_unlimited.config import Pool, Profile, save_pool
    save_pool(Pool(profiles=[Profile(id="a", name="Alice", kind="oauth", enabled=True)]))

    monkeypatch.setattr(cli, "_fetch_session_token", lambda host, port, profile_id, timeout=2.0: f"session-tok-for-{profile_id}")

    def boom(*a, **kw):
        raise AssertionError("must not fetch the shared placeholder token when --profile is given")

    monkeypatch.setattr(cli, "_fetch_placeholder_token", boom)

    assert cli.code(4317, [], profile_arg="Alice") == 0
    assert cli.os.environ["ANTHROPIC_AUTH_TOKEN"] == "session-tok-for-a"
    (binary, argv), = code_env
    assert os.path.basename(binary) == "claude"
    assert argv[0] == "claude"


def test_code_with_unknown_profile_flag_errors_without_launching(monkeypatch, code_env, capsys):
    from claude_unlimited.config import Pool, Profile, save_pool
    save_pool(Pool(profiles=[Profile(id="a", name="Alice", kind="oauth", enabled=True)]))

    assert cli.code(4317, [], profile_arg="does-not-exist") == 1
    assert code_env == []  # never launched claude
    assert "does-not-exist" in capsys.readouterr().err


def test_code_with_one_profile_never_prompts(monkeypatch, code_env):
    # Nothing to pick between, so the picker must not block on a single choice.
    from claude_unlimited.config import Pool, Profile, save_pool
    save_pool(Pool(profiles=[Profile(id="a", name="Alice", kind="oauth", enabled=True)]))

    def boom(prompt):
        raise AssertionError("must not prompt when there's only one enabled profile")

    monkeypatch.setattr("builtins.input", boom)
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port, timeout=2.0: "placeholder-tok")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    assert cli.code(4317, [], profile_arg=None) == 0
    assert cli.os.environ["ANTHROPIC_AUTH_TOKEN"] == "placeholder-tok"


def test_code_non_interactive_stdin_never_prompts_even_with_multiple_profiles(monkeypatch, code_env):
    from claude_unlimited.config import Pool, Profile, save_pool
    save_pool(Pool(profiles=[
        Profile(id="a", name="Alice", kind="oauth", enabled=True),
        Profile(id="b", name="Bob", kind="oauth", enabled=True),
    ]))

    def boom(prompt):
        raise AssertionError("must not prompt when stdin isn't a tty")

    monkeypatch.setattr("builtins.input", boom)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port, timeout=2.0: "placeholder-tok")

    assert cli.code(4317, [], profile_arg=None) == 0
    assert cli.os.environ["ANTHROPIC_AUTH_TOKEN"] == "placeholder-tok"


def test_code_self_heals_the_cli_launchers(monkeypatch, code_env):
    """Running `code` must (best-effort) create any missing CLI launcher — this
    is the reliable path that puts `cu` on PATH after an update, since it runs
    the freshly-installed code via the claude-unlimited symlink without needing
    the daemon to have restarted."""
    from claude_unlimited.config import Pool, Profile, save_pool
    save_pool(Pool(profiles=[Profile(id="a", name="Alice", kind="oauth", enabled=True)]))
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port, timeout=2.0: "tok")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    called = []
    monkeypatch.setattr(cli.updater, "ensure_cli_aliases", lambda *a, **kw: called.append(True))

    assert cli.code(4317, [], profile_arg=None) == 0
    assert called == [True]


# ---- live-state annotation ----

_FIXED_NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)


class _FixedDatetime(datetime):
    """Stands in for cli.datetime so `resets in Xh Ym` is exact instead of
    racing the wall clock between building the fixture and asserting on it."""

    @classmethod
    def now(cls, tz=None):
        return _FIXED_NOW if tz is not None else _FIXED_NOW.replace(tzinfo=None)


def _freeze_now(monkeypatch):
    monkeypatch.setattr(cli, "datetime", _FixedDatetime)


def _iso_in(**delta_kwargs):
    return (_FIXED_NOW + timedelta(**delta_kwargs)).isoformat()


def _iso_ago(**delta_kwargs):
    return (_FIXED_NOW - timedelta(**delta_kwargs)).isoformat()


def test_prompt_annotates_an_exhausted_profile_with_word_and_reset_time(monkeypatch, capsys):
    _freeze_now(monkeypatch)
    live = [{
        "id": "id-a", "state": "exhausted", "status_word": "exhausted",
        "usage_5h_percent": 100, "usage_5h_resets_at": _iso_in(hours=11, minutes=17),
        "usage_7d_percent": 40, "usage_7d_resets_at": _iso_in(days=3),
    }]
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    cli._prompt_profile_choice(_profiles(), live)
    out = capsys.readouterr().out
    assert "[2] Alice — exhausted · resets in 11h 17m" in out


def test_prompt_leaves_a_healthy_profile_as_a_bare_name(monkeypatch, capsys):
    live = [{"id": "id-a", "state": "eligible", "status_word": "healthy"}]
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    cli._prompt_profile_choice(_profiles(), live)
    out = capsys.readouterr().out
    assert "[2] Alice\n" in out
    assert "healthy" not in out


def test_prompt_with_no_live_snapshot_renders_bare_names(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    cli._prompt_profile_choice(_profiles(), None)
    out = capsys.readouterr().out
    assert "[2] Alice\n" in out
    assert "[3] Bob\n" in out


def test_prompt_renders_the_word_alone_when_there_is_no_reset_timestamp(monkeypatch, capsys):
    live = [{"id": "id-a", "state": "cooldown", "status_word": "cooldown"}]
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    cli._prompt_profile_choice(_profiles(), live)
    out = capsys.readouterr().out
    assert "[2] Alice — cooldown\n" in out


def test_prompt_omits_the_reset_clause_when_the_timestamp_is_in_the_past(monkeypatch, capsys):
    _freeze_now(monkeypatch)
    live = [{
        "id": "id-a", "state": "draining", "status_word": "almost exhausted",
        "usage_5h_percent": 90, "usage_5h_resets_at": _iso_ago(hours=2),
    }]
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    cli._prompt_profile_choice(_profiles(), live)
    out = capsys.readouterr().out
    assert "[2] Alice — almost exhausted\n" in out
    assert "resets in" not in out


def test_prompt_falls_back_to_a_bare_name_when_the_id_is_unknown(monkeypatch, capsys):
    live = [{"id": "not-a-known-id", "state": "exhausted", "status_word": "exhausted"}]
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    cli._prompt_profile_choice(_profiles(), live)
    out = capsys.readouterr().out
    assert "[2] Alice\n" in out


def test_picking_a_flagged_account_still_returns_that_profile(monkeypatch):
    live = [{"id": "id-a", "state": "exhausted", "status_word": "exhausted"}]
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    chosen = cli._prompt_profile_choice(_profiles(), live)
    assert chosen.name == "Alice"
    assert chosen.id == "id-a"


# ---- _profile_state_suffix ----

def test_profile_state_suffix_never_raises_on_a_malformed_entry():
    # A junk timestamp costs only the reset clause — the warning still shows.
    assert cli._profile_state_suffix(
        {"id": "x", "state": "exhausted", "usage_5h_resets_at": 12345}) == " \u2014 exhausted"
    # Anything not shaped like an entry at all degrades to a bare name.
    assert cli._profile_state_suffix("not-a-dict") == ""
    assert cli._profile_state_suffix(None) == ""
    assert cli._profile_state_suffix({}) == ""


def test_code_fetches_live_profiles_with_a_one_second_timeout_before_prompting(monkeypatch, code_env):
    from claude_unlimited.config import Pool, Profile, save_pool
    save_pool(Pool(profiles=[
        Profile(id="a", name="Alice", kind="oauth", enabled=True),
        Profile(id="b", name="Bob", kind="oauth", enabled=True),
    ]))
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port, timeout=2.0: "placeholder-tok")

    calls = []

    def fake_fetch(host, port, timeout=2.0):
        calls.append(timeout)
        return None

    monkeypatch.setattr(cli, "_fetch_live_profiles", fake_fetch)

    assert cli.code(4317, [], profile_arg=None) == 0
    assert calls == [1.0]


def test_unparseable_reset_timestamp_keeps_the_status_word(monkeypatch, capsys):
    """A malformed resets_at costs the reset clause, never the warning itself."""
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    live = [{"id": "id-a", "state": "exhausted", "status_word": "exhausted",
             "usage_5h_percent": 100, "usage_5h_resets_at": "not-a-timestamp"}]
    cli._prompt_profile_choice(_profiles(), live)
    out = capsys.readouterr().out
    assert "Alice — exhausted" in out
    assert "resets in" not in out


# ---- _profile_state_suffix: eligible-but-contradicts-"healthy" ----

def test_eligible_and_well_below_the_band_renders_bare():
    entry = {"id": "id-a", "state": "eligible", "status_word": "healthy",
              "usage_5h_percent": 26.0, "usage_7d_percent": 40.0}
    assert cli._profile_state_suffix(entry) == ""


def test_eligible_but_5h_in_band_renders_the_5h_window_and_percent():
    # default switch_threshold 98.0, band width 5.0 -> band starts at 93.0
    entry = {"id": "id-a", "state": "eligible", "status_word": "healthy",
              "usage_5h_percent": 95.0, "usage_7d_percent": 40.0}
    assert cli._profile_state_suffix(entry) == " — 5h at 95%"


def test_eligible_but_7d_in_band_renders_the_7d_window_and_percent():
    entry = {"id": "id-a", "state": "eligible", "status_word": "healthy",
              "usage_5h_percent": 26.0, "usage_7d_percent": 98.0}
    assert cli._profile_state_suffix(entry) == " — 7d at 98%"


def test_eligible_with_both_windows_in_band_shows_the_higher():
    entry = {"id": "id-a", "state": "eligible", "status_word": "healthy",
              "usage_5h_percent": 95.0, "usage_7d_percent": 98.0}
    assert cli._profile_state_suffix(entry) == " — 7d at 98%"


def test_eligible_uses_the_wire_window_labels_when_present():
    entry = {"id": "id-a", "state": "eligible", "status_word": "healthy",
              "usage_7d_percent": 98.0, "usage_window_label_7d": "1w"}
    assert cli._profile_state_suffix(entry) == " — 1w at 98%"


def test_non_eligible_state_rendering_is_unchanged_by_the_eligible_band_logic():
    entry = {"id": "id-a", "state": "exhausted", "status_word": "exhausted",
              "usage_5h_percent": 100, "usage_7d_percent": 98.0}
    assert cli._profile_state_suffix(entry) == " — exhausted"


def test_eligible_with_missing_switch_threshold_falls_back_to_the_default():
    # No switch_threshold on the entry at all -> DEFAULT_SWITCH_THRESHOLD (98.0)
    # governs the band, same as the explicit-98.0 case above.
    entry = {"id": "id-a", "state": "eligible", "status_word": "healthy",
              "usage_7d_percent": 98.0}
    assert cli._profile_state_suffix(entry) == " — 7d at 98%"


def test_eligible_with_missing_or_none_percents_renders_bare():
    assert cli._profile_state_suffix({"id": "id-a", "state": "eligible", "status_word": "healthy"}) == ""
    entry = {"id": "id-a", "state": "eligible", "status_word": "healthy",
              "usage_5h_percent": None, "usage_7d_percent": None}
    assert cli._profile_state_suffix(entry) == ""


def test_eligible_with_a_custom_switch_threshold_moves_the_band():
    # switch_threshold 80.0, band width 5.0 -> band starts at 75.0, so 76%
    # qualifies here even though it would be well clear of the 98.0 default.
    entry = {"id": "id-a", "state": "eligible", "status_word": "healthy",
              "switch_threshold": 80.0, "usage_5h_percent": 76.0}
    assert cli._profile_state_suffix(entry) == " — 5h at 76%"

    # And the same 76% does NOT qualify under the default threshold.
    entry_default = {"id": "id-a", "state": "eligible", "status_word": "healthy",
                       "usage_5h_percent": 76.0}
    assert cli._profile_state_suffix(entry_default) == ""


def test_usage_band_is_inclusive_at_its_exact_boundary():
    """The band is "at or above", not "above" — pin it, or a > / >= slip
    passes every other test in this file."""
    def suffix(pct):
        return cli._profile_state_suffix({
            "id": "id-a", "state": "eligible", "status_word": "healthy",
            "switch_threshold": 98.0, "usage_7d_percent": pct})

    assert suffix(92.999) == ""
    assert suffix(93.0) == " \u2014 7d at 93%"     # exactly threshold - band
    assert suffix(93.001) != ""
