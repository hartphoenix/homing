# environments.md — exact commands per environment

Read this in Phase 5, after the probe. Pick the section matching `probe.json.os` (or
`cloud_harness: true`). Everything here is a command you run, not advice you paraphrase.

Commands marked **[unverified]** were not measured on real hardware during research. Say so if
you rely on one; never assert it worked when you did not watch it work.

---

## 0. Rules that hold in every environment

**Touch-probe every path before you use it.** `~/.claude/skills` and `~/Library/LaunchAgents`
were both non-writable in a real probe on a machine where both obviously "should" have worked.
A path that exists is not a path you may write.

```sh
probe_dir() {  # probe_dir <dir> -> prints WRITABLE or DENIED, leaves nothing behind
  mkdir -p "$1" 2>/dev/null
  if touch "$1/.wprobe" 2>/dev/null; then rm -f "$1/.wprobe"; echo "WRITABLE $1"
  else echo "DENIED   $1"; fi
}
```

**Two kinds of destination, never mixed.** Markdown (`homing-check/SKILL.md`, `JUDGE.md`) goes
to a *skill directory*; scripts, config, state and logs go to the OS application-support / state
directories below. Never put a runnable script or a state file in a skill directory.

**Canonical skill install, then fan out by symlink.**

```sh
install -d -m 0755 "$HOME/.agents/skills"                       # canonical
# ...write homing-check/ here...
ln -sfn "$HOME/.agents/skills/homing-check" "$HOME/.claude/skills/homing-check"
```

Codex, Gemini CLI, Cursor, Copilot and OpenCode read `~/.agents/skills` natively; Claude Code
does **not** and needs the symlink. If the symlink target is not writable, install a second copy
there and record both paths plus a content hash in the manifest. On Windows without Developer
Mode, `New-Item -ItemType SymbolicLink` fails — copy and hash. Claude Code cloud sessions read
neither: they load account-level skills and the cloned repo's `.claude/skills/` only.

**The key never appears in a job definition.** `launchctl print`, `systemctl show`,
`schtasks /query /v` and `docker inspect` all print their environment dictionaries in cleartext
to any process of that user. The scheduler stores a *path to a script*; the script fetches the
value at run time into an unexported shell variable.

**`<state>/install-manifest.json` records every directory, file, symlink, scheduler identifier
and secret-store item created.** Uninstall reads it; uninstall never guesses.

---

## 1. macOS

### Paths (all under `~/Library/` — this is not a preference)

```
~/Library/Application Support/Homing/          config + bin      0700
~/Library/Application Support/Homing/state/    state             0700
~/Library/Logs/Homing/                         logs              0700
~/Library/LaunchAgents/com.homing.check.plist  scheduler         0644
~/.agents/skills/homing-check/                 generated skill
```

A launchd-spawned process has no Full Disk Access and no bundle identity, so TCC cannot even
show a consent dialog. Reads and writes under `~/Documents`, `~/Desktop`, `~/Downloads` and
`~/Library/Mobile Documents` (iCloud) fail with `Operation not permitted` — **silently, only
when the scheduler runs it**, never when you test by hand from a terminal that has FDA. If the
search genuinely needs a user folder, say plainly that background jobs are blocked from it and
keep your files in `~/Library/` anyway. Never ask anyone to grant a terminal Full Disk Access
for a housing search.

Check the config dir's `realpath`: refuse to install into iCloud Drive, Dropbox, OneDrive,
Syncthing, or a synced `Documents`.

### Scheduler — LaunchAgent only

**Never crontab on macOS.** Writing a crontab from a non-interactive process trips TCC
(`AUTHREQ_ATTRIBUTION`); the setuid `crontab` binary blocks on a consent dialog an unattended
installer can never answer, and the install hangs forever. `launchctl bootstrap` triggers no TCC
prompt at all.

**Never a LaunchDaemon.** Daemons run in the system session and cannot read the login keychain
(`errSecInternalComponent` / item-not-found, with `SessionCreate` either way).

`~/Library/LaunchAgents/com.homing.check.plist`, mode 0644, absolute paths only (launchd does
not expand `~`), and **no token anywhere in it**:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.homing.check</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>/Users/USER/Library/Application Support/Homing/bin/run.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <array><dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>37</integer></dict></array>
  <key>RunAtLoad</key><false/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key><string>/Users/USER</string>
  </dict>
  <key>WorkingDirectory</key><string>/Users/USER/Library/Application Support/Homing</string>
  <key>StandardOutPath</key><string>/Users/USER/Library/Logs/Homing/launchd.out.log</string>
  <key>StandardErrorPath</key><string>/Users/USER/Library/Logs/Homing/launchd.err.log</string>
  <key>ThrottleInterval</key><integer>300</integer>
  <key>ExitTimeOut</key><integer>30</integer>
  <key>ProcessType</key><string>Adaptive</string>
  <key>LowPriorityIO</key><true/>
</dict></plist>
```

`StartCalendarInterval` **only**: `StartInterval` silently drops a fire if the machine was
asleep *or the previous run was still going*, and the two keys are not aware of each other.
launchd's default `PATH` is only `/usr/bin:/bin:/usr/sbin:/sbin` — set it explicitly.
`RunAtLoad` stays false (Apple: "should be avoided"). Never set `KeepAlive` — it implies
`RunAtLoad` and turns a one-shot into a respawning daemon. `ProcessType Background` throttles
CPU and I/O hard; use `Adaptive`.

```sh
U=$(id -u); P="$HOME/Library/LaunchAgents/com.homing.check.plist"
plutil -lint "$P"                                    # validate BEFORE loading
launchctl bootstrap "gui/$U" "$P"                    # install, no sudo, no TCC prompt
launchctl kickstart -k "gui/$U/com.homing.check"     # run once now (required: see below)
launchctl print "gui/$U/com.homing.check" | grep -E 'state|runs|last exit code'
launchctl blame "gui/$U/com.homing.check"            # why it last started
```

Bootstrapping at 4pm a plist whose time was 9am catches up **nothing**; `runs` stays 0. Never
promise a catch-up run at install time — `kickstart` explicitly, and treat `runs = 0` after a
kickstart as a failed install. A job label is single-instance, so a fire arriving mid-run is
dropped and a long overrun silently loses the next day: keep the run short and keep the lock.

Never run `pmset repeat wakeorpoweron` on the user's behalf — it needs root and a machine holds
only one repeating pair, so setting it clobbers whatever is already there. To keep the Mac awake
*during* a run, wrap the work in `caffeinate -s` (no root needed).

### Secret store — `/usr/bin/security`, human-run

Write and read with the **same binary**. A language keyring library (Python `keyring`, Node
`keytar`) stamps the item's partition list with that interpreter's team ID; the next Homebrew
upgrade changes the signature and the job starts an un-dismissable 3am password-prompt storm
that "Always Allow" does not fix, because that edits the ACL, not the partition list.

The installer writes `<config>/set-token.sh` (0700, in a 0700 dir) and prints exactly one line
for the user to run: `bash "<config>/set-token.sh"`. It reads stdin with `IFS= read -rs`, feeds
`security add-generic-password -U -a "$USER" -s homing-api-token -w` the value **twice**
(prompt mode asks twice; every published one-liner that pipes once is wrong), and verifies by
HTTP status code alone against `__HOMING_ORIGIN__/api/v1/me/projects`. Never `-w <value>` (argv),
never `-p`, never `-A`.

Run-time retrieval, with a watchdog because a locked login keychain makes `security` sit on a
GUI prompt forever under launchd:

```sh
TOKEN=$(perl -e 'alarm shift; exec @ARGV' 20 \
  /usr/bin/security find-generic-password -a "$USER" -s homing-api-token -w 2>/dev/null) || {
    st=$?; [ "$st" -eq 44 ] && echo "homing: no key stored" >&2 || echo "homing: keychain unavailable ($st)" >&2
    exit 78; }
```

Exit 44 is `errSecItemNotFound`. Never export `TOKEN`; hand it to `curl` through `--config -` on
stdin so it never enters argv. If the *installer's own* keychain attempt returns
`Operation not permitted` or exit 152, that is the agent's sandbox, **not** a Mac without a
keychain — route to the human-run path, which is the correct path regardless.

### Pause / uninstall

```sh
U=$(id -u)
launchctl bootout "gui/$U/com.homing.check"          # PAUSE (keep the plist)
launchctl bootstrap "gui/$U" "$HOME/Library/LaunchAgents/com.homing.check.plist"   # RESUME
# UNINSTALL, in this order
launchctl bootout "gui/$U/com.homing.check" 2>/dev/null
rm -rf "$HOME/Library/Application Support/Homing/state/run.lock"
rm -f  "$HOME/Library/LaunchAgents/com.homing.check.plist"
security delete-generic-password -a "$USER" -s homing-api-token
rm -rf "$HOME/.agents/skills/homing-check" "$HOME/.claude/skills/homing-check"
```

`launchctl disable` alone does not reliably stop an already-loaded job in the current boot; use
`bootout` and keep the plist so resume is one command.

---

## 2. Linux

### Paths

```
${XDG_CONFIG_HOME:-$HOME/.config}/homing/          config + bin   0700
${XDG_STATE_HOME:-$HOME/.local/state}/homing/      state          0700
journald (SyslogIdentifier=homing-check)           logs
~/.config/systemd/user/homing-check.{service,timer}
~/.agents/skills/homing-check/
```

### Scheduler — systemd **user** timer

```ini
# ~/.config/systemd/user/homing-check.service
[Unit]
Description=Homing recurring search
After=network-online.target

[Service]
Type=oneshot
ExecStart=%h/.config/homing/bin/run.sh
WorkingDirectory=%h/.config/homing
RuntimeMaxSec=1200
TimeoutStopSec=30
LoadCredentialEncrypted=homing-api-token:%h/.config/homing/token.cred
StandardOutput=journal
StandardError=journal
SyslogIdentifier=homing-check
PrivateTmp=true
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=%h/.local/state/homing
```

```ini
# ~/.config/systemd/user/homing-check.timer
[Unit]
Description=Homing recurring search timer
[Timer]
OnCalendar=*-*-* 09:37:00
Persistent=true
RandomizedDelaySec=600
FixedRandomDelay=true
AccuracySec=1min
Unit=homing-check.service
[Install]
WantedBy=timers.target
```

```sh
systemd-analyze calendar '*-*-* 09:37:00'        # validate the expression FIRST
systemctl --user daemon-reload
systemctl --user enable --now homing-check.timer
loginctl enable-linger "$USER"                   # REQUIRED headless / logged-out
systemctl --user list-timers homing-check.timer  # NEXT / LEFT / LAST / PASSED
systemctl --user start homing-check.service      # run once now
journalctl --user -u homing-check.service -n 200 --no-pager
```

`Persistent=true` runs a missed occurrence once at next boot/login instead of skipping the day
(`OnCalendar=` only). Overlap prevention is free: a timer whose `Type=oneshot` unit is still
active does not restart it. `RuntimeMaxSec=` kills a hung run and marks the unit failed, which
is what makes `systemctl --user status` a real health signal. `loginctl enable-linger` is the
difference between "runs while the user has a session" and "runs after boot with nobody logged
in".

**cron fallback** — only when `systemctl --user is-system-running` gives nothing (Alpine/OpenRC,
minimal containers, WSL1, Termux):

```cron
37 9 * * * flock -n /run/user/1000/homing.lock -c '$HOME/.config/homing/bin/run.sh' >>$HOME/.local/state/homing/cron.log 2>&1
```

Install with `crontab -l > f; ...; crontab f` — never `crontab -` blind, it destroys existing
entries. State the weaknesses plainly: no catch-up when the machine was off, a minimal
environment (`PATH=/usr/bin:/bin`, no D-Bus session, so no keyring), and `%` must be escaped.

### Secret store — systemd credentials

The value never enters the environment, never appears in the unit, is exposed as a read-only
file, and is removed when the service stops.

```sh
# human-run, once
install -d -m 0700 ~/.config/homing
printf 'Paste your Homing key, then press Return: ' >&2; IFS= read -rs T; printf '\n' >&2
printf '%s' "$T" | systemd-creds encrypt --user --uid=self --name=homing-api-token - ~/.config/homing/token.cred
unset T; chmod 600 ~/.config/homing/token.cred
```

`--user` encryption needs systemd 256+ (`systemctl --version`); below that use plain
`LoadCredential=homing-api-token:%h/.config/homing/token` with a 0600 file. The runner reads
`TOKEN=$(cat "$CREDENTIALS_DIRECTORY/homing-api-token")` — never a hardcoded `/run/credentials`
path, never `EnvironmentFile=` (that puts it in `/proc/<pid>/environ`, inherited by every child).

**Never use `secret-tool`/libsecret for a scheduled job.** It needs a live Secret Service on the
D-Bus *session* bus; under cron or a lingering user unit you get `Cannot autolaunch D-Bus
without X11 $DISPLAY`, and `dbus-run-session --` "fixes" the test while leaving the real job
broken by spawning a fresh, empty bus.

Fallback 0600 file: create directory and file under `umask 077` in a subshell, or
`install -m 600 /dev/stdin`. `mkdir` then `chmod` leaves a real world-readable window.

### Pause / uninstall

```sh
systemctl --user disable --now homing-check.timer     # PAUSE, survives reboot
systemctl --user enable  --now homing-check.timer     # RESUME
# UNINSTALL
systemctl --user disable --now homing-check.timer
rm -f ~/.config/systemd/user/homing-check.{timer,service}
systemctl --user daemon-reload
systemctl --user reset-failed homing-check.service
systemctl --user clean --what=state homing-check.timer   # removes the Persistent= stamp
rm -f ~/.config/homing/token.cred
```

---

## 3. Windows

### Paths

```
%LOCALAPPDATA%\Homing\            config + bin
%LOCALAPPDATA%\Homing\state\      state
%LOCALAPPDATA%\Homing\logs\       logs
%USERPROFILE%\.agents\skills\homing-check\
```

### Scheduler — `Register-ScheduledTask`

```powershell
$Root   = "$env:LOCALAPPDATA\Homing"
$Action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Root\bin\run.ps1`"" `
  -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Daily -At 9:37AM
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
  -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
  -MultipleInstances IgnoreNew `
  -StartWhenAvailable `
  -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
  -WakeToRun:$false `
  -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName 'Homing\HomingCheck' `
  -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force
```

Management:

```powershell
Start-ScheduledTask   -TaskName 'Homing\HomingCheck'      # run now
Get-ScheduledTask     -TaskName 'Homing\HomingCheck' | Get-ScheduledTaskInfo
  # LastRunTime, LastTaskResult (0 = ok), NextRunTime, NumberOfMissedRuns
Disable-ScheduledTask -TaskName 'Homing\HomingCheck'      # PAUSE
Enable-ScheduledTask  -TaskName 'Homing\HomingCheck'      # RESUME
Unregister-ScheduledTask -TaskName 'Homing\HomingCheck' -Confirm:$false   # UNINSTALL
```

`-RunLevel Limited`, never `Highest`. Never put the key in `-Argument`: the task XML under
`C:\Windows\System32\Tasks\` is readable text and `schtasks /query /v` prints the full command
line.

### The logon-type / DPAPI decision — pick one, out loud

| Shape | Runs signed out? | Secret store that works | Cost |
|---|---|---|---|
| `-LogonType Interactive` | no | **DPAPI file** (`ConvertFrom-SecureString`) | task does not run while signed out |
| `-LogonType S4U` | yes | Credential Manager `CredRead` only | **DPAPI is unavailable** — no user master key |
| `-LogonType Password` | yes | DPAPI | stores the Windows password; never do this |

S4U + DPAPI is the number-one "worked on my machine" Windows bug: it fails **only** at scheduled
runtime, never in an interactive test. Default to **`Interactive` + DPAPI** and say the plain
sentence: *"This runs while you're signed in to this PC."* Use `S4U` only when the probe finds a
readable Credential Manager path already installed — `cmdkey` cannot read a secret back at all,
and installing the `CredentialManager` module means a PSGallery trust prompt you should not
create for a non-technical user. **[unverified: S4U + CredRead was not exercised.]**

### Secret store — DPAPI, human-run

```powershell
$dir = Join-Path $env:LOCALAPPDATA 'Homing'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
$file = Join-Path $dir 'token.dpapi'
$sec = Read-Host -AsSecureString 'Paste your Homing key, then press Enter'
$sec | ConvertFrom-SecureString | Set-Content -Path $file -Encoding ascii -NoNewline
icacls $file /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
Invoke-RestMethod -Uri '__HOMING_ORIGIN__/api/v1/me/projects' -Authentication Bearer -Token $sec | Out-Null
```

`Read-Host -AsSecureString` keeps it out of PSReadLine history. `ConvertFrom-SecureString`
without `-Key` is DPAPI, keyed to this user on this machine — the file is worthless if copied.
At run time hand the `SecureString` straight to `Invoke-RestMethod -Authentication Bearer
-Token`; never materialise it into a string.

---

## 4. Always-on cloud harness

### Container

Prefer a **supervisor loop** over a cron daemon: most slim images ship no cron, and container
cron inherits an empty environment.

```sh
#!/bin/sh
set -eu
INTERVAL="${HOMING_INTERVAL_SEC:-86400}"
trap 'exit 0' TERM INT
while :; do
  START=$(date +%s)
  timeout -k 30 1200 /opt/homing/bin/run.sh || echo "run exited $?"
  ELAPSED=$(( $(date +%s) - START )); SLEEP=$(( INTERVAL - ELAPSED ))
  [ "$SLEEP" -lt 60 ] && SLEEP=60
  sleep "$SLEEP"
done
```

Overlap is structurally impossible; the hung-run bound is `timeout -k`; restart policy comes
from the orchestrator (`restart: unless-stopped`). If cron is required, write
`/etc/cron.d/homing` with `37 9 * * *`, send output to `>/proc/1/fd/1 2>/proc/1/fd/2`, and run
`cron -f` under `tini`.

Secret: a **mounted file**, read inside `run.sh`. Compose `secrets:` (mounted at
`/run/secrets/<name>`), never `environment:` — env vars show in `docker inspect`, are inherited
by every child, and bake into image layers if set via `ENV`/`ARG`. Never render a compose file
into your own output unredacted: `docker compose config` interpolates `env_file` values inline.
Verify secret files by shape (`stat`), never by content.

### Claude Code cloud Routine

Only when `/schedule` is present (hidden on Console API keys, Bedrock/Vertex, or when
`DISABLE_TELEMETRY` / `DO_NOT_TRACK` / `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` /
`DISABLE_GROWTHBOOK` is set). Minimum interval one hour.

**Mandatory extra step:** Edit routine → environment → Network access → Custom → add the Homing
host. The Default environment is Trusted and allows only Anthropic's package-registry allowlist;
otherwise every Homing request 403s with `x-deny-reason: host_not_allowed` **while the routine
still shows green**. Green means the session started and exited without an infrastructure error
— nothing more. The success signal is the Homing run record, never the badge.

Secret: a **dedicated** cloud environment's variables, not a shared Team environment (docs: they
are "visible to anyone who uses the environment").

### GitHub Actions

`schedule: - cron: '37 9 * * *'` (UTC), plus `workflow_dispatch: {}` for a Run-now button,
`concurrency: {group: homing-search, cancel-in-progress: false}`, `timeout-minutes: 20`,
`permissions: {contents: read}`, and the secret in `env:` of the single step that needs it.
Keep the repo private and the triggers `schedule` + `workflow_dispatch` only.

Say the two costs out loud before choosing it: fires can be delayed 5–30 minutes or dropped
under load, and **in a public repo a scheduled workflow is auto-disabled after 60 days with no
repository activity** — a silent failure with one email to whoever last enabled it. Secret
masking is exact-substring only: anything that base64s, JSON-wraps or URL-encodes the value
stops being masked.

Whatever remote host is chosen, mint a **separate key for that worker** so revoking it does not
break the local one, record its issue date in `<state>/state.json`, and tell the user in their
own words that someone else's computer now holds a key to their searches — and that turning it
off is one click in Homing.

---

## 5. No durable scheduler

When nothing writable answers — no LaunchAgent dir, no systemd, no Task Scheduler, no cloud
harness — **do not fake it**. Install the on-demand runner: `homing-check` in the skill
directory, `run.sh` in the config directory, no scheduler artifact, and one plain sentence:
*"I couldn't find a way to run this on its own here. I can search whenever you ask — just say
'check Homing'."* The same applies at isolation rung 0 even when a scheduler exists.

Never substitute Claude Code's `/loop` for a scheduler: it is session-scoped, dies with the
conversation, and a recurring task auto-expires seven days after creation. Claude Code Desktop
scheduled tasks (`~/.claude/scheduled-tasks/<name>/SKILL.md`) are durable but fire only while
the app is open and the machine awake, and a missed 9am can be caught up at 11pm — pick a
Desktop task **or** a LaunchAgent, never both.

---

## 6. Locking

The portable lock is an atomic `mkdir` **directory** containing a PID file, with a staleness
check. `mkdir` is atomic on every POSIX filesystem including NFS.

**macOS ships neither `flock` nor `timeout`.** Do not emit `flock` in a cross-platform runner.
And `trap ... EXIT` is not SIGKILL-safe — a stale lock left by a killed run has already silently
disabled a real Homing job for a full day, exiting 0 every morning with nothing in the log.

```sh
LOCK="$STATE/run.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  stale=0
  if [ -f "$LOCK/pid" ] && ! kill -0 "$(cat "$LOCK/pid" 2>/dev/null)" 2>/dev/null; then stale=1; fi
  # belt and braces: a lock dir older than 2x the run bound is stale regardless of pid reuse
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +40 2>/dev/null)" ]; then stale=1; fi
  if [ "$stale" -eq 1 ]; then rm -rf "$LOCK"; mkdir "$LOCK" 2>/dev/null || { echo "locked"; exit 0; }
  else echo "already running; exiting cleanly"; exit 0; fi
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT INT TERM
```

Exit **0** when another copy holds the lock — "already running" is not a failure and must not
light up a health check.

Portable timeout, since macOS has no `timeout`:

```sh
run_bounded() {  # run_bounded <seconds> <cmd...>; exit 142 on timeout
  secs="$1"; shift
  if command -v timeout  >/dev/null 2>&1; then timeout  -k 30 "$secs" "$@"; return $?; fi
  if command -v gtimeout >/dev/null 2>&1; then gtimeout -k 30 "$secs" "$@"; return $?; fi
  perl -e 'alarm shift; exec @ARGV' "$secs" "$@"
}
```

Three independent layers, all of them: the scheduler's own single-instance behaviour, this
lock, and Homing's five-minute run lease (the only one that prevents *cross-machine* overlap).

---

## 7. Logging

| OS | Path |
|---|---|
| macOS | `~/Library/Logs/Homing/run-YYYYmmdd-HHMMSS.log` (also visible in Console.app) |
| Linux | journald via `SyslogIdentifier=homing-check`; rotation is already solved |
| Windows | `%LOCALAPPDATA%\Homing\logs\` plus `Microsoft-Windows-TaskScheduler/Operational` |
| Container | stdout → the orchestrator's log driver |

launchd's `StandardOutPath` never rotates; the runner must. Directory 0700, files 0600, set by
`umask 077` before the first redirect — not by `chmod` after.

```sh
umask 077
LOG="$LOGDIR/run-$(date +%Y%m%d-%H%M%S).log"
find "$LOGDIR" -type f -name 'run-*.log' -mtime +14 -delete
```

Redaction filter on everything going to disk — a backstop, not the primary control:

```sh
redact() { sed -E \
  -e 's/(Bearer|Authorization:)[[:space:]]*[A-Za-z0-9._~+/=-]{8,}/\1 <redacted>/g' \
  -e 's/(st_live_|sk-ant-|ghp_|github_pat_)[A-Za-z0-9._-]{8,}/\1<redacted>/g' \
  -e 's/(claim_token"?[[:space:]]*[:=][[:space:]]*"?)[^",[:space:]]+/\1<redacted>/g'; }
run_bounded 1200 "$BIN/phase.sh" 2>&1 | redact >>"$LOG"
```

Never `set -x` in a script that touches the key. Never `curl -v` — it prints the
`Authorization:` header to stderr, which lands in the launchd log, which the agent then reads.
Never log raw listing HTML: untrusted, large, and its ANSI escapes corrupt any terminal that
later `cat`s the log. Log structured fields — source, id, url, decision.

---

## 8. Isolation rungs

Climb to the highest rung the probe finds, and record which one in `config.json`.

| Rung | What it is | Where you get it |
|---|---|---|
| 1 | Narrow tool surface: two verbs (`homing.py`, `sources.py`), no raw shell, no `curl`/`wget`, no subagent spawn, no reads of `bin/**` | every runtime with a permission config |
| 2 | Restricted working directory | Claude Code `--add-dir` scope; Codex `--sandbox workspace-write --add-dir`; Gemini `--sandbox` |
| 3 | **Egress allowlist enforced outside the model** | Claude Code `sandbox.enabled` + `allowUnsandboxedCommands: false` + `network.allowedDomains`; a container behind an egress proxy |
| 5 | Container | any cloud harness |
| 6 | Managed harness with network config | Claude Code cloud Routine with Custom network access |

Rung ≥3 → full unattended capability. Rung 1–2 only → keep the wrapper gates, halve the write
budget, prefer pull sources (feeds, sitemaps, official APIs) over page fetching. Rung 0 → **no
unattended runner at all**; install the on-demand runner and say so.

**The honest note about blanket-approval flags.** Some schedulers can only invoke an agent by
turning every approval off. Never emit `--dangerously-skip-permissions`,
`--permission-mode bypassPermissions`, `--dangerously-bypass-approvals-and-sandbox`, `--yolo`,
`-y`, or `--force` into a scheduled job. Safe forms: `claude -p --permission-mode dontAsk`;
`codex exec --sandbox workspace-write --approve-for-me`; `gemini --approval-mode default
--policy <file> --sandbox`. **If the only way to make a runtime unattended is a flag whose name
contains "dangerous", "yolo", "bypass", or "skip-permissions", do not install a schedule for
that runtime.**

Two related facts. A Claude Code cloud Routine has no permission-mode picker and no approval
prompts, and can use every tool from an included connector — writes included — without asking;
strip every connector it does not need. And Claude Code disables trust verification under `-p`,
which every scheduled run is, so the first-run trust dialog is not a control that exists here.
Where the chosen rung still lets the runner read other files on the machine, tell the user
exactly that, in one sentence.

---

## 9. Uninstall and pause

Uninstall is driven by `<state>/install-manifest.json` and must be idempotent. Order matters:

1. Disable the schedule, so nothing fires mid-teardown.
2. Kill any in-flight run and `rm -rf` the lock directory.
3. **If this worker currently holds a claimed Homing run, complete it with
   `status: failed` and the summary `worker uninstalled`.** Otherwise the project stays locked
   for up to five minutes and the next worker sees a confusing `409`.
4. Delete the scheduler artifact (plist / unit files / task / routine).
5. Delete state; offer to keep logs.
6. Delete the secret-store item.
7. Hand the user the URL to disconnect the key in Homing — the agent cannot revoke its own
   token — and print exactly what was removed.

Pause has two levels; name the second one to the user. Locally: `bootout` /
`systemctl --user disable --now` / `Disable-ScheduledTask`. Server-side: Homing's own pause,
which the runner honours by exiting immediately. Server-side pause is the one that still works
when the user is not in front of the machine, so it is the one to name in the report.

Write `<state>/UNINSTALL.md` with the literal commands for the environment you actually chose —
not a menu of all five.

---

## 10. Multi-worker lanes

Default is **one worker**. A second exists only when the probe found two runtimes with durable
schedulers *and* the user chose it.

A **lane** is a stable slug for one legitimate access path to one source — `daft:sitemap`,
`listingsproject:rss`, `nyc-opendata:socrata`, `streeteasy:manual`, `email:alerts:zillow`.

**Lanes are assigned statically at install, to the narrowest capable worker.** Residential-only
and inbox lanes (`residential_required`) go to the **local** worker — the user's own machine,
own IP, own inbox. Lanes needing a person go to neither and surface as a task. Everything else
goes to the cloud worker if one exists, else local. The result is baked into each worker's
`sources.json` as a literal lane list, so two workers never contend: neither knows how to run
the other's lanes. No leader election, no distributed lock, no clock agreement.

Stagger the schedules. Never `:00`, never `:30` (Claude Code jitters recurring fires up to 30
minutes; GitHub Actions drops peak-of-hour load). Two workers never share a minute — e.g. cloud
at `:07` on even hours, local at `:37` on odd hours.

Only the **write** phase holds Homing's run lease, and it targets under 60 seconds. Read and
search hold nothing.

**`409 run_already_claimed` means park the batch and back off — never "skip this project".**
Skipping throws away search work already paid for in tokens and network. Write the batch to
`<state>/parked/<project>/<lane>-<sha256>.json` (0600) with its intended idempotency key, move
to the next project immediately, and retry with jittered backoff 5 / 15 / 45 / 120 s (±25%,
≤4 min). Still claimed → exit 0 as "deferred", not a failure. Every run drains `parked/` before
the search phase. A batch older than about six days is replayed with a **fresh** idempotency key
and a byte-identical payload; never bump `observed_at` on drain. A peer holding a live lease is
normal and must never appear in user-facing output.

Handoff is by observation only. A cloud worker reports what it could not reach in
`continuation.needs_local`; the local worker reads recent run events, takes the newest run whose
`agent_label` starts with `homing/cloud-`, and unions that list into its own lane set for that
cycle — a missed cycle self-heals on the next cloud run. Local silent >72 h → the cloud worker
appends one project comment, at most once per 7 days. Cloud silent >24 h → the local worker
takes every lane it can legitimately serve, says so in its summary, and reverts automatically.

A worker that finds nothing still completes a run. "Ran, found nothing" and "didn't run" must
stay distinguishable, and the run record is the only place that distinction lives.
