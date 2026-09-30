#!/usr/bin/env bash
set -euo pipefail

# `claude-unlimited purge` is the thorough path: it also removes each Profile's
# credential from the OS keystore, which needs the config that names those
# Profiles to still exist. This script only handles the case where the CLI is
# already gone or broken.
#
# Look for the installed launcher by path first: ~/.local/bin is often not on
# PATH (a fresh account), and a PATH-only lookup would skip the purge and leave
# the stored credentials behind. The install's own venv copy goes first, then
# the symlink in ~/.local/bin, then whatever is on PATH.
INSTALL_ROOT="$HOME/.local/share/claude-unlimited"
LAUNCHER=""
for candidate in \
  "$INSTALL_ROOT/venv/bin/claude-unlimited" \
  "$HOME/.local/bin/claude-unlimited" \
  "$(command -v claude-unlimited 2>/dev/null || true)"; do
  if [ -n "$candidate" ] && [ -f "$candidate" ] && [ -x "$candidate" ]; then
    LAUNCHER="$candidate"
    break
  fi
done
if [ -n "$LAUNCHER" ]; then
  # Not `exec`: a launcher that cannot start at all (its interpreter is gone)
  # must fall through to the manual removal below. Any other exit status is
  # purge's own answer — including declining its confirmation — and is final.
  purge_status=0
  "$LAUNCHER" purge "$@" || purge_status=$?
  case "$purge_status" in
    126|127) echo "$LAUNCHER could not be run (exit $purge_status)." ;;
    *) exit "$purge_status" ;;
  esac
fi

echo "The claude-unlimited command isn't available — removing files directly."
echo "NOTE: stored credentials cannot be removed this way, because the config"
echo "that names them is about to go. Remove entries with the service prefix"
echo "'claude-unlimited.oauth.' from your OS keystore by hand if you want them gone."
echo

# Tear down the background service first, so its files aren't orphaned. Without
# this the Linux systemd --user unit stays enabled and pointed at a python that
# no longer exists — producing exec failures on every login — and the macOS
# LaunchAgent stays registered too.
case "$(uname -s)" in
  Linux)
    if command -v systemctl >/dev/null 2>&1; then
      systemctl --user disable --now claude-unlimited.service 2>/dev/null || true
      rm -f "$HOME/.config/systemd/user/claude-unlimited.service"
      systemctl --user daemon-reload 2>/dev/null || true
    fi
    command -v loginctl >/dev/null 2>&1 && loginctl disable-linger "$(id -un)" 2>/dev/null || true
    ;;
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/com.claude-unlimited.daemon.plist"
    if [ -f "$PLIST" ]; then
      launchctl bootout "gui/$(id -u)/com.claude-unlimited.daemon" 2>/dev/null || true
      rm -f "$PLIST"
    fi
    # The HUD is a separate app with its own login item. Left behind, it keeps
    # starting on every login and polling a daemon that is no longer there.
    HUD_PLIST="$HOME/Library/LaunchAgents/ai.devdock.claude-unlimited.hud.plist"
    if [ -f "$HUD_PLIST" ]; then
      launchctl bootout "gui/$(id -u)/ai.devdock.claude-unlimited.hud" 2>/dev/null || true
      rm -f "$HUD_PLIST"
    fi
    pkill -f "HUD - Heads-Up Display.app/Contents/MacOS/" 2>/dev/null || true
    rm -rf "$HOME/Applications/HUD - Heads-Up Display.app" "$HOME/Applications/CapacityWidget.app"
    ;;
esac

# Both command names ship — removing only `claude-unlimited` left `cu` behind
# as a dangling symlink.
rm -f "$HOME/.local/bin/claude-unlimited" "$HOME/.local/bin/cu"
rm -rf "$HOME/.local/share/claude-unlimited" "$HOME/.claude-unlimited"
echo "Removed. ~/.claude was left untouched."
