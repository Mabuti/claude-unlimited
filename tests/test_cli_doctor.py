import os
import sys

import pytest

import claude_unlimited.cli as cli
import claude_unlimited.daemon_installer as daemon_installer
import claude_unlimited.i18n as i18n
import claude_unlimited.profiles as profile_repo


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    # doctor self-heals the CLI launchers; never touch the real ~/.local/bin.
    monkeypatch.setattr(cli.updater, "ensure_cli_aliases", lambda *a, **kw: None)
    # Nor may it query the OS service manager or (re)install the HUD.
    monkeypatch.setattr(daemon_installer, "status",
                        lambda: {"installed": False, "running": False, "pid": None})
    monkeypatch.setattr(cli.hud_installer, "is_supported", lambda: False)


def test_doctor_reports_notifications_availability(env, capsys):
    cli.doctor()
    out = capsys.readouterr().out
    assert "Desktop notifications:" in out


def test_doctor_reports_service_not_installed(env, monkeypatch, capsys):
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": False, "running": False, "pid": None})
    cli.doctor()
    out = capsys.readouterr().out
    assert "Background service: not installed" in out


def test_doctor_reports_service_installed_and_running(env, monkeypatch, capsys):
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": True, "running": True, "pid": 42})
    cli.doctor()
    out = capsys.readouterr().out
    assert "Background service: installed — running (pid 42)" in out


def test_doctor_reports_available_languages(env, capsys):
    cli.doctor()
    out = capsys.readouterr().out
    langs = i18n.list_locales()
    for code in langs:
        assert code in out
    assert "current: en" in out


# --- CLI launchers: another `cu` on PATH must not count as ours ------------
#
# The real installed shape: ~/.local/bin/{claude-unlimited,cu} are symlinks into
# <install>/venv/bin/, and `cu` is also the name of a foreign system tool.

def _script(path, text="#!/bin/sh\nexit 0\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


@pytest.fixture
def launchers(env, monkeypatch, tmp_path):
    if sys.platform == "win32":
        pytest.skip("POSIX symlinks and extensionless scripts")
    install = tmp_path / "install"
    venv_bin, bin_dir = install / "venv" / "bin", tmp_path / "home" / ".local" / "bin"
    foreign = tmp_path / "usr" / "bin"
    for name in ("claude-unlimited", "cu"):
        _script(venv_bin / name)
    _script(foreign / "cu")
    bin_dir.mkdir(parents=True)
    monkeypatch.setattr(cli.updater, "INSTALL_ROOT", install)
    monkeypatch.setattr(cli.updater, "VENV_SCRIPTS", venv_bin)
    monkeypatch.setattr(cli.updater, "BIN_DIR", bin_dir)

    def link_ours(*names):
        for name in names:
            (bin_dir / name).symlink_to(venv_bin / name)

    def set_path(*dirs):
        monkeypatch.setenv("PATH", os.pathsep.join(str(d) for d in dirs))

    class Shape:
        pass
    shape = Shape()
    shape.venv_bin, shape.bin_dir, shape.foreign = venv_bin, bin_dir, foreign
    shape.link_ours, shape.set_path = link_ours, set_path
    return shape


def _doctor(capsys):
    code = cli.doctor()
    lines = [ln for ln in capsys.readouterr().out.splitlines()
             if ln.startswith("CLI launchers:")]
    assert len(lines) == 1
    return code, lines[0]


OK_LINE = "CLI launchers: OK — claude-unlimited and cu both on PATH"


def test_doctor_launchers_ok_when_both_are_our_symlinks(launchers, capsys):
    launchers.link_ours("claude-unlimited", "cu")
    launchers.set_path(launchers.bin_dir)
    assert _doctor(capsys) == (0, OK_LINE)


def test_doctor_foreign_cu_file_in_our_bin_dir_is_not_ours(launchers, capsys):
    launchers.link_ours("claude-unlimited")
    _script(launchers.bin_dir / "cu", "#!/bin/sh\necho my-personal-cu\n")
    launchers.set_path(launchers.bin_dir)
    code, line = _doctor(capsys)
    assert code == 0  # a warning: the installer aborts on a failing doctor
    assert line.startswith("CLI launchers: claude-unlimited OK — but `cu` runs ")
    assert str(launchers.bin_dir / "cu") in line
    assert "left alone" in line and "claude-unlimited doctor" in line


def test_doctor_cu_symlinked_to_a_foreign_tool_is_not_ours(launchers, capsys):
    launchers.link_ours("claude-unlimited")
    (launchers.bin_dir / "cu").symlink_to(launchers.foreign / "cu")
    launchers.set_path(launchers.bin_dir)
    code, line = _doctor(capsys)
    assert code == 0
    assert "`cu` runs " in line and "not ours" in line
    assert "left alone" in line
    assert "OK — claude-unlimited and cu" not in line


def test_doctor_foreign_dir_earlier_on_path_suggests_reordering(launchers, capsys):
    launchers.link_ours("claude-unlimited", "cu")
    launchers.set_path(launchers.foreign, launchers.bin_dir)
    code, line = _doctor(capsys)
    assert code == 0
    assert f"`cu` runs {launchers.foreign / 'cu'}, not ours" in line
    assert f"put {launchers.bin_dir} before {launchers.foreign} on PATH" in line
    assert "left alone" not in line


def test_doctor_no_cu_of_ours_anywhere_does_not_suggest_moving_a_directory(launchers, capsys):
    (launchers.venv_bin / "cu").unlink()
    launchers.link_ours("claude-unlimited")
    launchers.set_path(launchers.bin_dir, launchers.foreign)
    code, line = _doctor(capsys)
    assert code == 0
    assert "not ours" in line and "before" not in line
    assert line.endswith("use `claude-unlimited`")


def test_doctor_cu_elsewhere_symlinked_to_our_venv_cu_is_ours(launchers, capsys, tmp_path):
    launchers.link_ours("claude-unlimited")
    other = tmp_path / "other-bin"
    other.mkdir()
    (other / "cu").symlink_to(launchers.venv_bin / "cu")
    launchers.set_path(launchers.bin_dir, other)
    assert _doctor(capsys) == (0, OK_LINE)


def test_doctor_cu_beside_claude_unlimited_is_ours_outside_the_install(launchers, capsys, tmp_path):
    # e.g. a pip/dev venv, or Windows' WindowsApps: both launchers together.
    other = tmp_path / "other-venv" / "bin"
    _script(other / "claude-unlimited")
    _script(other / "cu")
    launchers.set_path(other)
    assert _doctor(capsys) == (0, OK_LINE)


def test_doctor_keeps_the_missing_message_when_cu_is_absent(launchers, capsys):
    launchers.link_ours("claude-unlimited")
    launchers.set_path(launchers.bin_dir)
    code, line = _doctor(capsys)
    assert code == 1
    assert line == ("CLI launchers: claude-unlimited on PATH — cu missing "
                    "(ensure ~/.local/bin is on your PATH)")


def test_doctor_shadowed_cu_without_claude_unlimited_does_not_suggest_it(launchers, capsys):
    launchers.set_path(launchers.foreign)  # only a foreign `cu`; none of ours
    code = cli.doctor()
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("CLI launchers:")]
    assert code == 1
    assert lines == [
        "CLI launchers: none on PATH — claude-unlimited missing "
        "(ensure ~/.local/bin is on your PATH)",
        f"CLI launchers: `cu` runs {launchers.foreign / 'cu'}, not ours",
    ]
