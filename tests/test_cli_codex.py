"""`claude-unlimited codex` / `cu codex`: launches the real Codex CLI with its
model provider pointed at this daemon, so Codex sessions are served by the
pool's codex-kind (ChatGPT/Codex) accounts.

No test here runs the real `codex` binary or touches the network: execvp/
execvpe, _run_tool, shutil.which, _ensure_daemon, the token fetchers and the
Pool are all mocked or isolated to a tmp_path config directory."""
import os
import types

import pytest

import claude_unlimited.cli as cli
from claude_unlimited.config import Pool, Profile, Settings, save_pool


def _codex_profile(pid="c", name="ChatGPT", **kw):
    return Profile(id=pid, name=name, kind="codex", priority=1, automatic=True, enabled=True, **kw)


def _oauth_profile(pid="a", name="Alice", **kw):
    return Profile(id=pid, name=name, kind="oauth", priority=1, automatic=True, enabled=True,
                    account_uuid="u", **kw)


@pytest.fixture
def codex_env(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli, "_ensure_daemon", lambda port: True)
    # Isolate load_pool()/save_pool() (used by both cli.py and config.launch_argv)
    # to a throwaway directory -- never the real ~/.claude-unlimited config.
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    # load_pool()'s own Path.home() fallback (for shared_claude_dir) would
    # otherwise try to build a WindowsPath on a POSIX runner the moment a
    # test monkeypatches os.name to "nt" -- a pre-existing pathlib quirk
    # documented in test_launcher_config.py, not something this ticket owns.
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))

    execs = []
    monkeypatch.setattr(cli.os, "execvpe", lambda file, args, env: execs.append((file, args, env)))

    def _run_tool(argv, **kw):
        # Windows has no execvpe: codex() falls back to _run_tool as a child
        # process. Stubbing only execvpe leaves that branch making a real
        # subprocess launch of a fake path on Windows.
        execs.append((cli._resolve_launcher(argv[0]), argv, kw.get("env")))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli, "_run_tool", _run_tool)
    return execs


def _ensure_daemon_must_not_be_called(monkeypatch):
    def boom(port):
        raise AssertionError("must not start the daemon before the executable check passes")
    monkeypatch.setattr(cli, "_ensure_daemon", boom)


# ---- argv shape ----

def test_full_argv_order_pinned_profile(monkeypatch, codex_env):
    save_pool(Pool(profiles=[_codex_profile()]))
    monkeypatch.setattr(cli, "_fetch_session_token",
                         lambda host, port, profile_id, timeout=2.0: f"session-tok-for-{profile_id}")

    def boom(*a, **kw):
        raise AssertionError("must not fetch the placeholder token when --profile is given")
    monkeypatch.setattr(cli, "_fetch_placeholder_token", boom)

    assert cli.codex(4317, ["exec", "hi"], profile_arg="ChatGPT") == 0
    (exe, argv, env), = codex_env
    assert os.path.basename(exe) == "codex"

    expected = [
        "codex",
        "-c", 'model_provider="claude_unlimited"',
        "-c", 'model_providers.claude_unlimited.name="Claude Unlimited"',
        "-c", 'model_providers.claude_unlimited.base_url="http://127.0.0.1:4317/v1"',
        "-c", 'model_providers.claude_unlimited.wire_api="responses"',
        "-c", 'model_providers.claude_unlimited.env_key="CLAUDE_UNLIMITED_TOKEN"',
        "-c", "model_providers.claude_unlimited.requires_openai_auth=false",
        "-c", "model_providers.claude_unlimited.supports_websockets=false",
        "exec", "hi",
    ]
    assert argv == expected
    assert env["CLAUDE_UNLIMITED_TOKEN"] == "session-tok-for-c"


def test_argv_matches_the_shared_overrides_helper(monkeypatch, codex_env):
    """The overrides list codex() builds must be exactly what
    _codex_provider_overrides() returns -- the one place tests and docs share
    the wire format."""
    save_pool(Pool(profiles=[_codex_profile()]))
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port, timeout=2.0: "placeholder-tok")

    assert cli.codex(4317, [], profile_arg=None) == 0
    (_exe, argv, _env), = codex_env
    assert argv == ["codex", *cli._codex_provider_overrides(4317)]


def test_rotating_token_path_uses_the_placeholder_token(monkeypatch, codex_env):
    save_pool(Pool(profiles=[_codex_profile()]))
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port, timeout=2.0: "placeholder-tok")

    def boom(*a, **kw):
        raise AssertionError("must not fetch a session token without --profile")
    monkeypatch.setattr(cli, "_fetch_session_token", boom)

    assert cli.codex(4317, [], profile_arg=None) == 0
    (_exe, _argv, env), = codex_env
    assert env["CLAUDE_UNLIMITED_TOKEN"] == "placeholder-tok"


# ---- refusals ----

def test_no_codex_profile_refuses_without_launching(monkeypatch, codex_env, capsys):
    save_pool(Pool(profiles=[_oauth_profile()]))  # only a non-codex Profile enabled

    assert cli.codex(4317, [], profile_arg=None) == 1
    assert codex_env == []
    err = capsys.readouterr().err
    assert "No enabled Codex/ChatGPT account" in err
    assert "add-codex-account" in err


def test_no_profiles_at_all_refuses_without_launching(monkeypatch, codex_env, capsys):
    save_pool(Pool(profiles=[]))

    assert cli.codex(4317, [], profile_arg=None) == 1
    assert codex_env == []


def test_profile_flag_matching_a_non_codex_profile_is_refused(monkeypatch, codex_env, capsys):
    save_pool(Pool(profiles=[_oauth_profile(), _codex_profile()]))

    assert cli.codex(4317, [], profile_arg="Alice") == 1
    assert codex_env == []
    err = capsys.readouterr().err
    assert "not a Codex account" in err
    assert "Alice" in err


def test_profile_flag_matching_nothing_is_refused(monkeypatch, codex_env, capsys):
    save_pool(Pool(profiles=[_codex_profile()]))

    assert cli.codex(4317, [], profile_arg="does-not-exist") == 1
    assert codex_env == []
    err = capsys.readouterr().err
    assert "does-not-exist" in err


def test_missing_codex_binary_refuses_before_starting_the_daemon(monkeypatch, codex_env, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    _ensure_daemon_must_not_be_called(monkeypatch)

    assert cli.codex(4317, [], profile_arg=None) == 1
    assert codex_env == []
    err = capsys.readouterr().err
    assert "codex" in err.lower()
    assert "https://github.com/openai/codex" in err


# ---- Windows branch ----

def test_windows_branch_runs_via_run_tool_and_returns_its_code(monkeypatch, codex_env):
    save_pool(Pool(profiles=[_codex_profile()]))
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port, timeout=2.0: "placeholder-tok")
    monkeypatch.setattr(cli.os, "name", "nt")

    def _run_tool(argv, **kw):
        codex_env.append((argv[0], argv, kw.get("env")))
        return types.SimpleNamespace(returncode=7)

    monkeypatch.setattr(cli, "_run_tool", _run_tool)

    assert cli.codex(4317, [], profile_arg=None) == 7
    (_exe, argv, env), = codex_env
    assert argv[0] == "codex"
    assert env["CLAUDE_UNLIMITED_TOKEN"] == "placeholder-tok"


# ---- configured launcher ----

def test_configured_launcher_extra_args_are_honoured(monkeypatch, codex_env):
    save_pool(Pool(profiles=[_codex_profile()],
                    settings=Settings(launchers={"codex": "codex --extra-flag"})))
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port, timeout=2.0: "tok")

    assert cli.codex(4317, ["exec"], profile_arg=None) == 0
    (_exe, argv, _env), = codex_env
    assert argv[0] == "codex"
    # Configured extra args sit after the overrides and before the passthrough.
    overrides = cli._codex_provider_overrides(4317)
    assert argv == ["codex", *overrides, "--extra-flag", "exec"]


# ---- passthrough of flag-looking args ----

def test_passthrough_of_flag_looking_args_via_main(monkeypatch, codex_env):
    save_pool(Pool(profiles=[_codex_profile()]))
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port, timeout=2.0: "tok")

    assert cli.main(["codex", "exec", "--model", "gpt-5.6-luna", "hi"]) == 0
    (_exe, argv, _env), = codex_env
    assert argv[-4:] == ["exec", "--model", "gpt-5.6-luna", "hi"]


def test_account_flag_is_parsed_by_argparse_not_passed_through(monkeypatch, codex_env):
    save_pool(Pool(profiles=[_codex_profile(pid="c", name="ChatGPT")]))
    monkeypatch.setattr(cli, "_fetch_session_token",
                         lambda host, port, profile_id, timeout=2.0: f"tok-{profile_id}")

    assert cli.main(["codex", "--account", "ChatGPT", "exec", "hi"]) == 0
    (_exe, argv, env), = codex_env
    assert "--account" not in argv
    assert "ChatGPT" not in argv
    assert argv[-2:] == ["exec", "hi"]
    assert env["CLAUDE_UNLIMITED_TOKEN"] == "tok-c"


@pytest.mark.parametrize("args", [
    ["--profile", "fast", "exec", "hi"],
    ["-p", "fast", "exec", "hi"],
    ["exec", "--profile", "fast", "hi"],
    ["exec", "-p", "fast", "hi"],
])
def test_codex_own_profile_flag_passes_through_untouched(monkeypatch, codex_env, args):
    # Codex's -p/--profile picks $CODEX_HOME/<name>.config.toml; it is not a
    # pin, even when a pool account happens to share the name.
    save_pool(Pool(profiles=[_codex_profile(pid="c", name="fast")]))
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port, timeout=2.0: "placeholder-tok")

    def boom(*a, **kw):
        raise AssertionError("codex's own --profile must not pin the session")
    monkeypatch.setattr(cli, "_fetch_session_token", boom)

    assert cli.main(["codex", *args]) == 0
    (_exe, argv, env), = codex_env
    assert argv[-len(args):] == args
    assert env["CLAUDE_UNLIMITED_TOKEN"] == "placeholder-tok"


def test_everything_after_a_double_dash_goes_to_codex_verbatim(monkeypatch, codex_env):
    save_pool(Pool(profiles=[_codex_profile(pid="c", name="ChatGPT")]))
    monkeypatch.setattr(cli, "_fetch_session_token",
                         lambda host, port, profile_id, timeout=2.0: f"tok-{profile_id}")

    assert cli.main(["codex", "--account", "ChatGPT", "exec", "--",
                     "--port", "9", "--account", "x", "--", "-p", "fast"]) == 0
    (_exe, argv, env), = codex_env
    overrides = cli._codex_provider_overrides(4317)
    assert argv == ["codex", *overrides, "exec", "--port", "9", "--account", "x", "--", "-p", "fast"]
    assert env["CLAUDE_UNLIMITED_TOKEN"] == "tok-c"


def test_an_unknown_account_names_the_account_flag(monkeypatch, codex_env, capsys):
    save_pool(Pool(profiles=[_codex_profile()]))

    assert cli.main(["codex", "--account", "nope"]) == 1
    assert "--account 'nope'" in capsys.readouterr().err


def test_double_dash_split_leaves_other_commands_alone():
    assert cli._split_codex_passthrough(["code", "--", "-p"]) == (["code", "--", "-p"], [])
    assert cli._split_codex_passthrough([]) == ([], [])
    assert cli._split_codex_passthrough(["codex", "exec"]) == (["codex", "exec"], [])


# ---- nothing written under CODEX_HOME ----

def test_never_sets_codex_home_or_writes_under_it(monkeypatch, codex_env, tmp_path):
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))
    save_pool(Pool(profiles=[_codex_profile()]))
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port, timeout=2.0: "tok")

    assert cli.codex(4317, [], profile_arg=None) == 0
    (_exe, _argv, env), = codex_env
    assert "CODEX_HOME" not in env
    assert not (tmp_path / ".codex").exists()


# ---- interactive picker offers codex profiles only ----

def test_picker_with_multiple_codex_profiles_offers_only_codex_profiles(monkeypatch, codex_env):
    save_pool(Pool(profiles=[
        _oauth_profile(pid="a", name="Alice"),
        _codex_profile(pid="c1", name="ChatGPT One"),
        _codex_profile(pid="c2", name="ChatGPT Two"),
    ]))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_fetch_live_profiles", lambda host, port, timeout=1.0: None)
    monkeypatch.setattr(cli, "_fetch_session_token",
                         lambda host, port, profile_id, timeout=2.0: f"tok-{profile_id}")

    prompted = {}

    def fake_input(prompt):
        prompted["prompt"] = prompt
        return "2"  # first (and only) codex profile offered after "Rotated accounts"

    monkeypatch.setattr("builtins.input", fake_input)

    assert cli.codex(4317, [], profile_arg=None) == 0
    (_exe, _argv, env), = codex_env
    assert env["CLAUDE_UNLIMITED_TOKEN"] == "tok-c1"


def test_single_codex_profile_never_prompts(monkeypatch, codex_env):
    save_pool(Pool(profiles=[_oauth_profile(), _codex_profile()]))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port, timeout=2.0: "placeholder-tok")

    def boom(prompt):
        raise AssertionError("must not prompt with only one enabled codex profile")
    monkeypatch.setattr("builtins.input", boom)

    assert cli.codex(4317, [], profile_arg=None) == 0
