# Linux and WSL: the login keyring, and what happens after a reboot

This applies to any Linux install, and especially to **WSL**, where the failure mode
below is close to guaranteed rather than occasional.

## What the tool needs and why

On Linux, Claude Unlimited stores every OAuth token in your **Secret Service**
provider — in practice, the **GNOME login keyring** (`gnome-keyring-daemon`), read and
written through the `secret-tool` CLI (package `libsecret-tools`). This needs a running
D-Bus session and an *unlocked* keyring collection; if either is missing, every token
lookup blocks or fails.

The fix below also needs `systemd --user`. On WSL that is not on by default: set
`systemd=true` under `[boot]` in `/etc/wsl.conf` and restart the distro (`wsl --shutdown`
from Windows). Without it the installer falls back to a session-only daemon and none of
the units below exist. The README's WSL install notes cover the same prerequisite.

## The symptom

After a reboot (or, on a real desktop, sometimes after a suspend/resume), you may see:

- an **"Unlock Login Keyring"** dialog appearing repeatedly, or never appearing at all
  on a headless/WSL session where nothing can show it;
- the dashboard stuck on **"Loading…"**;
- every request failing with something like
  `429 … every configured account is exhausted or cooling down`;
- and, confusingly, running `claude-unlimited reauth` reports **"No OAuth Profile
  currently needs re-authentication"** — while every request still 429s.

Read that last line for what it is: **nothing is actually exhausted.** The daemon
cannot reach its own credential store, and it currently reports that failure the same
way it reports a normal rate-limit cooldown, so accounts cycle
`eligible → cooldown → eligible` forever instead of being marked as needing
re-auth. `reauth` only acts on accounts already marked `auth_invalid`, so it has
nothing to do and says so truthfully. This is a known defect in how the failure is
classified, tracked for a fix — until it lands, treat "every account exhausted"
immediately after a reboot as a locked-keyring symptom first, not a quota problem.

## Why it happens on WSL specifically

A normal desktop login (GDM, LightDM, GNOME on Wayland/X11, …) unlocks your login
keyring automatically via PAM, using your login password, at the same moment you log
in. **WSL has no display manager and no PAM login step of that kind** — you get a shell
with no keyring-unlock ever having happened. The first thing that touches the keyring
(here, `secret-tool` on the daemon's behalf) either blocks on a GUI prompt that has
nowhere to render, or finds the collection locked and gets nothing back.

The same "no GUI to hand off to" gap shows up in the browser login flow too: on WSL,
`claude-unlimited add-account` (and `add-codex-account`) does not open a browser window
for you, because there is no browser integration between WSL and the Windows desktop by
default. You need to copy the login URL it prints and open it yourself in a Windows
browser, then paste the resulting authorization code back into the terminal. This is a
property of the WSL environment, not something the unlock unit below changes.

## The permanent fix

The fix is a small `systemd --user` unit that unlocks the keyring non-interactively,
ordered to run **before** the daemon starts, plus a drop-in on the daemon's own unit so
it waits for that to finish.

You need a **non-interactive password source** you control — your password manager's
CLI, or (as a fallback) a file you create yourself with `chmod 600` holding just the
keyring password. **Do not use `secret-tool` itself as that source** — it reads from
the very keyring this script exists to unlock, which is circular and will not work on
a cold boot.

### 1. The unlock script

Save as `~/.local/bin/claude-unlimited-keyring-unlock`, `chmod +x` it, and replace the
`PASSWORD_CMD` line with your own non-interactive password source.

```bash
#!/bin/bash
# Unlock the GNOME login keyring non-interactively so claude-unlimited can
# read its OAuth tokens without a human at a dialog. Idempotent. Safe to run
# by hand or from a systemd --user unit at login.
#
# Why this exists: on WSL nothing unlocks the login keyring at sign-in, so
# after every reboot the daemon came up, blocked on a GUI password prompt, and
# every request failed with "No eligible Profile".
#
# Why it stops claude-unlimited first: the daemon polls secret-tool constantly,
# and the instant gnome-keyring is restarted it D-Bus-activates a competing
# instance that loads the keyring LOCKED and wins the org.freedesktop.secrets
# name. Measured, not theoretical.
set -u
# --- Defaults to a 0600 file holding just the password (see section 4 to
# create it). Any command that prints the keyring password and nothing else
# works instead — swap in your password manager's CLI if you have one:
#   PASSWORD_CMD='pass show gnome-keyring/login'
# Never use secret-tool here: it reads the keyring this script unlocks.
PASSWORD_CMD='cat ~/.config/claude-unlimited/keyring-password'
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

locked() { busctl --user call org.freedesktop.secrets /org/freedesktop/secrets/collection/login \
             org.freedesktop.DBus.Properties Get ss org.freedesktop.Secret.Collection Locked 2>/dev/null | awk '{print $NF}'; }

if [ "$(locked)" = "false" ]; then echo "login keyring already unlocked"; exit 0; fi

# Fetch the password BEFORE touching anything. If the source fails, exit without
# killing the keyring — otherwise Restart=on-failure on the daemon unit would
# re-run this every 2s and hammer gnome-keyring forever.
pw="$(eval "$PASSWORD_CMD" 2>/dev/null | tr -d '\n')"
if [ -z "$pw" ]; then echo "FAILED: PASSWORD_CMD printed nothing" >&2; exit 1; fi

was_active=0
if systemctl --user is-active --quiet claude-unlimited.service; then
  was_active=1; systemctl --user stop claude-unlimited.service
fi
# Retry loop: even with the daemon stopped, a straggling secret-tool (an orphan
# of the daemon's polling) can D-Bus-activate a competing gnome-keyring in the
# gap between our pkill and our launch, and it loads the keyring LOCKED and
# wins the name. So after launching, insist on BOTH: collection unlocked AND
# exactly one keyring daemon. Re-roll the window if not. Measured.
daemons() { pgrep -fc '^(/usr/bin/)?gnome-keyring-daemon'; }
ok=0
for attempt in 1 2 3; do
  pkill -f '^/usr/libexec/gcr-prompter' 2>/dev/null || true
  pkill -f '^secret-tool' 2>/dev/null || true
  pkill -f '^gnome-keyring-daemon|^/usr/bin/gnome-keyring-daemon' 2>/dev/null || true
  sleep 1
  # Password travels stdin -> stdin. Never argv, never a file, never echoed.
  # systemd-run --scope puts the daemonized keyring in its own cgroup, so it
  # survives `systemctl stop claude-unlimited` when this runs as ExecStartPre.
  printf '%s\n' "$pw" \
    | systemd-run --user --scope --quiet --unit="gnome-keyring-cu-$(date +%s%N)" \
        gnome-keyring-daemon --unlock --components=secrets --daemonize >/dev/null 2>&1
  sleep 2
  if [ "$(locked)" = "false" ] && [ "$(daemons)" -eq 1 ]; then ok=1; break; fi
  echo "attempt $attempt: locked=$(locked) daemons=$(daemons) — retrying" >&2
done
unset pw

rc=0
if [ "$ok" = 1 ]; then echo "login keyring unlocked"
else echo "FAILED: login keyring still locked after 3 attempts" >&2; rc=1; fi

[ "$was_active" = 1 ] && systemctl --user start --no-block claude-unlimited.service
exit $rc
```

A few details worth understanding, not just copying:

- **`pkill` patterns are `^`-anchored.** `pkill -f` matches against the full command
  line, including the command line of the shell that is running this very script — an
  unanchored pattern that happens to match your own invocation kills your own shell.
- **The daemon is stopped *before* gnome-keyring is touched, and started again only
  after the unlock is verified.** The daemon polls `secret-tool` continuously; if
  gnome-keyring is killed and restarted while the daemon is still running, the daemon's
  own polling can D-Bus-activate a *second*, locked instance of the keyring daemon
  within about a second, and that instance wins the `org.freedesktop.secrets` bus name
  — so you end up locked again immediately, and it looks like the unlock never worked.
  Stop → fix → verify → start, in that order, every time.
- **`gnome-keyring-daemon --unlock` exits `0` whether or not it actually unlocked
  anything.** Its exit code is not a trustworthy signal. The only thing that reliably
  tells you the collection's real state is the D-Bus `Locked` property, which is what
  the `locked()` helper above checks — always verify through that, not through the
  unlock command's exit status.
- **`start --no-block`** avoids a real, reproduced deadlock. The daemon's unit is
  ordered `After=` this one. If the script (still running as this unit's `ExecStart`)
  calls a *blocking* `systemctl start` on the daemon, systemd queues that start to wait
  for this unit to finish — which is waiting for the script — which is waiting for the
  start. `--no-block` queues the job and lets the script exit.
- **`ExecStartPre=` in the drop-in** is what makes a plain daemon restart heal a locked
  keyring. `Wants=` only *starts* the unlock unit if it isn't already active, and a
  `RemainAfterExit` oneshot stays active after its first run — so after boot, `Wants=`
  never fires it again. Running the script as `ExecStartPre` re-checks on every start;
  it exits immediately when nothing is wrong.
- **The verify-and-retry loop** exists because a straggling `secret-tool` — an orphan of
  the daemon's polling — can D-Bus-activate a competing keyring in the gap between the
  `pkill` and our launch, even with the daemon stopped. The competitor loads the keyring
  locked and wins the name. So the script insists on *both* `Locked=false` *and* exactly
  one keyring daemon, and re-rolls the window up to three times otherwise. Reproduced.
- **`systemd-run --scope`** around the keyring launch keeps the daemonized
  `gnome-keyring-daemon` out of the daemon unit's cgroup. Without it, when the script
  runs as `ExecStartPre` the keyring lands in `claude-unlimited.service`'s cgroup, and
  `systemctl stop claude-unlimited` takes the keyring down with it.

### 2. The unlock unit

Save as `~/.config/systemd/user/claude-unlimited-keyring-unlock.service`:

```ini
[Unit]
Description=Unlock the GNOME login keyring (claude-unlimited reads its tokens from it)
Before=claude-unlimited.service

[Service]
Type=oneshot
RemainAfterExit=yes
# The daemonized gnome-keyring lands in this cgroup; never kill it on unit stop.
KillMode=process
ExecStart=%h/.local/bin/claude-unlimited-keyring-unlock

[Install]
WantedBy=default.target
```

`KillMode=process` matters here: the default kill mode would tear down the whole
cgroup — including the `gnome-keyring-daemon` process this unit just spawned and
detached with `--daemonize` — the moment the oneshot unit is considered "stopped".
`process` kills only the unit's own tracked process, letting the daemonized keyring
process keep running independently.

### 3. The drop-in on the daemon's own unit

Save as `~/.config/systemd/user/claude-unlimited.service.d/keyring.conf`:

```ini
# Drop-in: survives `claude-unlimited install`/updates regenerating the main
# unit file. Start only after the login keyring is unlocked, or every request
# 429s with a misleading "exhausted" message.
[Unit]
After=claude-unlimited-keyring-unlock.service
Wants=claude-unlimited-keyring-unlock.service

# Re-check on EVERY daemon start, not just at login. Wants= does not re-run a
# RemainAfterExit unit that is already active, so without this a keyring that
# locked after boot would survive a daemon restart. The script exits 0
# immediately when already unlocked.
[Service]
# Leading "-": a failed unlock is logged and IGNORED, so the daemon still starts
# (and 429s, as before) instead of crash-looping. Without the "-", the daemon
# unit's Restart=on-failure + StartLimitIntervalSec=0 would re-run this every 2s
# forever on a wrong password.
ExecStartPre=-%h/.local/bin/claude-unlimited-keyring-unlock
```

A **drop-in** (a `.d/` directory next to the unit's own filename, holding one or more
`.conf` files) is systemd's mechanism for adding directives to a unit without editing
the unit file itself. It survives the installer/updater regenerating
`claude-unlimited.service` wholesale, because the drop-in lives in its own file and is
merged in at load time regardless of what the main unit file says.

### 4. First run — this creates the keyring

On a fresh distro there is no `login` keyring yet and no password. Create the file the
default `PASSWORD_CMD` reads from before the first run:

```bash
mkdir -p ~/.config/claude-unlimited
(umask 077; head -c 32 /dev/urandom | base64 | tr -d '\n' > ~/.config/claude-unlimited/keyring-password)
```

That file is the keyring's password from now on — keep it with the same care as
`~/.claude`. It's deliberately outside `~/.claude-unlimited/`, which `purge` deletes, so
a purge-and-reinstall doesn't orphan the keyring. If it's ever lost, the keyring can't be
unlocked: delete `~/.local/share/keyrings/login.keyring` and re-add every account.

Then run the script **once by hand**. `gnome-keyring-daemon --unlock` creates the login
keyring with the supplied password when none exists, so the first run bootstraps it;
every later run just unlocks it.

```bash
chmod 700 ~/.local/bin/claude-unlimited-keyring-unlock
~/.local/bin/claude-unlimited-keyring-unlock        # prints "login keyring unlocked"
```

Then enable the unit and reload so the drop-in is picked up:

```bash
systemctl --user daemon-reload
systemctl --user enable --now claude-unlimited-keyring-unlock.service
```

### Order of operations, end to end

1. `systemd=true` in `/etc/wsl.conf`, restart the distro.
2. Install the tool (`./install.sh` from your checkout). This is what creates
   `claude-unlimited.service`; the drop-in below has nothing to attach to before this.
3. Save the script, the unit, and the drop-in (sections 1–3).
4. Create the password file (section 4) — `mkdir -p ~/.config/claude-unlimited` and
   write a random password to `~/.config/claude-unlimited/keyring-password`.
5. Run the script once by hand (section 4) — creates and unlocks the keyring.
6. `daemon-reload`, `enable --now` the unlock unit, then verify (next section).
7. `claude-unlimited add-account`. On WSL the browser will not open by itself — paste
   the URL it prints into a Windows browser and paste the code back.

Doing `add-account` before step 5 is what produces the "choose a password for a new
keyring" dialog — and a password you set in a dialog is one nothing can supply at boot.

## Verifying it works without rebooting

You don't need to reboot to test this. Lock the collection by hand, restart the
daemon's unit, and confirm the keyring comes back unlocked:

```bash
# Lock it (Lock lives on the Service interface and takes an array of collection paths;
# the Collection interface has no Lock method)
busctl --user call org.freedesktop.secrets /org/freedesktop/secrets \
  org.freedesktop.Secret.Service Lock ao 1 /org/freedesktop/secrets/collection/login

# Restart the daemon. Its ExecStartPre= runs the unlock script first.
systemctl --user restart claude-unlimited.service

# Confirm it actually unlocked
busctl --user call org.freedesktop.secrets /org/freedesktop/secrets/collection/login \
  org.freedesktop.DBus.Properties Get ss org.freedesktop.Secret.Collection Locked
```

The last command should print `v b false`. If you skipped the `ExecStartPre=` line in
the drop-in, restarting the daemon will **not** unlock anything (see the `Wants=` note
above) — run `systemctl --user restart claude-unlimited-keyring-unlock.service` instead.
Also confirm the keyring survives the daemon stopping: `systemctl --user stop
claude-unlimited.service`, then the `Locked` query again — still `false`, and
`pgrep -af keyring-d` still shows one daemon. This proves the units fire correctly against a
simulated lock — it does **not** prove behavior across a real reboot, since a reboot
also re-creates the D-Bus session and `XDG_RUNTIME_DIR` from scratch. Treat "verified
against a simulated lock" and "verified across a real reboot" as two different claims,
and don't rely on this until you've seen a real reboot come back clean.

## If you got here too late (recovering lost tokens)

If the keyring was reset, its password was lost, or it's otherwise unrecoverable:

1. **Preserve the old keyring file — rename it, don't delete it**, e.g.
   `mv ~/.local/share/keyrings/login.keyring ~/.local/share/keyrings/login.keyring.locked-$(date +%F)`.
   You may want to come back to it, and a rename costs nothing.
2. **Stop the daemon first** (`systemctl --user stop claude-unlimited.service`) —
   same reasoning as above: touching the keyring while the daemon is polling it risks
   a competing locked instance winning the bus name.
3. Let a fresh, empty keyring collection be created (it will be, the next time
   something asks for one and none exists), or create one deliberately with your
   distribution's keyring tooling.
4. Verify it's unlocked (`Locked` = `false` via the `busctl` call above) before
   restarting the daemon.
5. **Re-add every account** with `claude-unlimited add-account` /
   `claude-unlimited add-codex-account` — this means a real browser login for each
   one, since the tokens that lived in the old keyring are gone. On WSL, remember the
   browser won't open for you (see above) — copy the printed URL into a Windows
   browser by hand and paste the code back.

Two things *won't* rescue you here, and it's worth knowing why before you try them:

- **The isolated per-account credential file
  (`claude-accounts/<id>/.credentials.json`) is not a durable backup.** It holds
  whatever refresh token was current the moment the account was added. Anthropic
  rotates the refresh token every time it's used, and the daemon writes the *rotated*
  token only into the keyring — never back into that file. So that file is a one-shot
  recovery at best: it works only if the account has never refreshed since it was
  added, and goes stale the moment it has.
- **`claude-unlimited reauth` will not help until the profile is marked
  `auth_invalid`.** As covered above, a missing/locked credential currently surfaces
  as an ordinary cooldown, not as `auth_invalid` — so `reauth` has nothing to act on
  and will (truthfully, unhelpfully) report that nothing needs re-authentication.
  Don't wait on it; go straight to re-adding the accounts.

## Security note

A keyring unlocked from a password held in a mode-0600 file is protected by exactly
the same file permissions that protect a mode-0600 plaintext credentials file. Stated
plainly: this is not a material security upgrade over plaintext-at-rest — it satisfies
the tool's requirement for a Secret Service provider to exist, and it does keep the
token out of the keyring's own on-disk format when the file itself isn't compromised,
but it does not remove your OS user account as the trust boundary. If your password
manager offers a proper CLI with its own locking, prefer that over a bare file.
