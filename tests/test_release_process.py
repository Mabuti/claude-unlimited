"""Guards on the release pipeline's shape. Releases here are immutable once
published, so the order of these steps is not a style choice: publishing
before the HUD is attached ships a release that can never get one."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_ci_creates_a_draft_never_a_published_release():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "gh release create" in workflow
    assert "--draft" in workflow


def test_the_publish_script_checks_before_it_publishes():
    script = (ROOT / "scripts" / "publish_release.sh").read_text(encoding="utf-8")
    upload = script.index("gh release upload")
    publish = script.index("--draft=false")
    assert script.index("isDraft") < upload < publish
    assert script.index("gh release download") < publish   # round-trip digest check first
    assert "HUD.sha256" in script and "lipo -archs" in script


def test_the_release_build_is_universal():
    build = (ROOT / "macos-widget" / "build.sh").read_text(encoding="utf-8")
    assert "--arch arm64 --arch x86_64" in build
    # One trap covers everything: a later `trap … EXIT` replaces an earlier one.
    assert "trap cleanup EXIT" in build
    assert 'cleanup() { rm -rf "$GENERATED"' in build


def test_the_installer_checks_with_its_own_bin_dir_on_path():
    # A fresh account has no ~/.local/bin on PATH. doctor finds the launchers
    # there, so a bare call fails and aborts the install before the service,
    # HUD and dashboard steps.
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'if ! PATH="$BIN_DIR:$PATH" "$CLI" doctor; then' in script


def test_the_uninstaller_finds_the_launcher_without_relying_on_path():
    # Without this a fresh account skips `purge` and leaves credentials behind.
    script = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
    venv = script.index('"$INSTALL_ROOT/venv/bin/claude-unlimited"')
    local_bin = script.index('"$HOME/.local/bin/claude-unlimited"')
    on_path = script.index("command -v claude-unlimited")
    assert venv < local_bin < on_path
    assert '[ -f "$candidate" ] && [ -x "$candidate" ]' in script


def test_the_uninstaller_falls_back_only_when_the_launcher_cannot_run():
    script = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
    # Not exec'd: a launcher whose interpreter is gone must reach the manual
    # removal, while purge's own exit status (a declined prompt is 1) is final.
    assert 'exec "$LAUNCHER"' not in script
    assert '"$LAUNCHER" purge "$@" || purge_status=$?' in script
    assert "126|127)" in script
    assert '*) exit "$purge_status" ;;' in script
    assert script.index("126|127)") < script.index("removing files directly")
