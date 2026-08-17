# troubleshooting.md — repair, re-run, uninstall, and the silent failures

Read this when the search stopped working, when Phase 1 finds a prior install, or when
`selftest.py` fails. Start with these three questions, in order. They have three different
answers and conflating them is the most common wrong turn.

1. **Did it run?** Ask the scheduler, not the log.
   `launchctl print gui/$(id -u)/com.homing.check | grep -E 'state|runs|last exit code'` ·
   `systemctl --user list-timers homing-check.timer; systemctl --user status homing-check.service` ·
   `Get-ScheduledTask -TaskName 'Homing\HomingCheck' | Get-ScheduledTaskInfo`
2. **Did it finish?** `<state>/last-run.json` — the only run artifact a model may read.
3. **Did Homing see it?** The project's run history at `__HOMING_ORIGIN__`. A worker that finds
   nothing still completes a run, so "no run record" and "ran, found nothing" are different
   facts.

**Re-run by hand:** `<config>/bin/run.sh` (or `run.ps1`) — the same command the scheduler uses,
no arguments. Never re-run the installer to test a run: a scheduled fire must never load the
installer, and Phase 8 asserts it.

**Repair:** read `<state>/install-manifest.json`, verify every path in it still exists with the
recorded mode, re-register only what is missing, re-run `selftest.py`. Never build a second
install alongside a broken one. If the manifest is gone the install is unmanaged — tear it down
with `<state>/UNINSTALL.md` and start over.

**Uninstall** follows `environments.md` §9 in order. Step 3 is the one people skip: if this
worker holds a claimed Homing run, complete it as `failed` with the summary
`worker uninstalled`, or the project stays locked five minutes and the next worker meets a
confusing `409`. The agent cannot revoke its own key — hand the user the Homing URL and say why.

---

## Silent failures, by symptom

**Every morning exits 0 with an empty log; `runs` climbs; nothing reaches Homing.**
*Stale lock.* A run was SIGKILLed (sleep, reboot, `pkill`) and `trap … EXIT` never fired, so
`<state>/run.lock` still holds a dead PID. Every later run sees the directory, prints
`already running`, exits 0 — correctly, forever. This has already silently disabled a real
Homing job for a full day. Confirm: `ls -ld <state>/run.lock; cat <state>/run.lock/pid;
kill -0 <pid>`. Fix: `rm -rf <state>/run.lock`, re-run by hand. Permanent fix: the lock needs a
PID file **and** an age check (`environments.md` §6) — a trap alone is not SIGKILL-safe.

**Works when you run it in Terminal, fails only under the scheduler, macOS.**
*TCC denial.* A launchd job has no Full Disk Access and no bundle identity, so anything under
`~/Documents`, `~/Desktop`, `~/Downloads` or iCloud fails with `Operation not permitted` — and
your terminal has FDA, which is exactly why the by-hand test passes. Confirm:
`log show --predicate 'subsystem == "com.apple.TCC"' --last 1h --info | grep -i homing`.
Fix: move every path the job touches under `~/Library/`, update the plist and the manifest, then
`launchctl bootout` + `bootstrap` + `kickstart`. Never ask the user to grant Full Disk Access.

**Windows task shows `LastTaskResult 0x80070569`, or the key reads only when run by hand.**
*S4U.* `-LogonType S4U` has no DPAPI user master key, so `ConvertTo-SecureString` fails at
scheduled runtime and never in an interactive test; `0x80070569` is the related LSA credential
drop after some updates. Fix, in preference order: re-register with `-LogonType Interactive` and
tell the user "this runs while you're signed in", or keep S4U and move the key to Credential
Manager. Re-registering also clears the LSA case.

**A cloud Routine is green, but Homing has no runs.**
*`host_not_allowed`.* The Default cloud environment is Trusted and allows only Anthropic's
package-registry allowlist; Homing requests 403 with `x-deny-reason: host_not_allowed` while the
badge stays green — green only means the session started and exited. Fix: Edit routine →
environment → Network access → **Custom** → add the Homing host, then run it once manually and
check the run record, not the badge.

**GitHub Actions stopped firing after about two months.**
*60-day inactivity disable.* In a public repository a scheduled workflow is disabled when no
repository activity has occurred in 60 days; the notice is one email to whoever last enabled it,
and reports say the disable hits the workflow's other triggers too. Only new commits reset the
timer — issues, tags and releases do not. Fix: re-enable it in the Actions tab, then either move
the schedule off Actions or have the job push a monthly heartbeat commit. For a non-technical
user this is a standing maintenance cost and a good reason not to pick Actions.

**Every write 401s.**
*Key expired or revoked.* Stop all writes. Do **not** retry, loop, or prompt for a key. After
two consecutive 401s, disable the timer and send exactly one notification, ever: "Homing needs
you to reconnect." Fix: pair again, which mints and stores a new key. Rotation order matters:
create new → store → verify with a GET → **then** revoke the old one. Never revoke first.

**`409 run_already_claimed` on every project, run after run.**
*Lease held, or a crashed run left one.* Homing's run lease is five minutes. Normal case: the
peer holds it — park the batch to `<state>/parked/<project>/`, move on, retry with jittered
backoff 5/15/45/120 s, and if still claimed exit 0 as "deferred". Never a failure, never shown
to the user. Abnormal case: read the run list — if the holder is *this* worker from a crashed
invocation and the lease has expired, reclaim it. Never respond by skipping the project. Check
`<state>/parked/` is draining: files older than a day mean the drain step is not running.

**Homing shows nothing for days and the scheduler shows `runs = 0`.**
*It never fired.* On macOS, bootstrapping a plist at 4pm whose time was 9am catches up nothing —
launchd coalesces only intervals passing while the job is bootstrapped. On Linux without
`loginctl enable-linger`, a user timer stops at logout. A Claude Code Desktop task fires only
while the app is open. Fix: `launchctl kickstart -k gui/$(id -u)/com.homing.check` (treat
`runs = 0` afterwards as a failed install), `loginctl enable-linger "$USER"`, or move to a
scheduler that survives how the machine is actually used.

**One source reports zero results every run and the run still says "ok".**
*A silent zero is a lie to someone looking for a home.* Only `EMPTY-GENUINE` — every one of that
source's `shell_markers` present and zero listings — may be reported as "nothing new". Missing
shell markers, a body under `min_ok_bytes`, a final URL outside the requested path family, or a
vendor marker all mean **"couldn't check this source"**. Confirm from `<state>/sources-state.json`:
`status`, `vendor`, `consecutive_blocks`, `next_eligible`. Fix: let the retirement ladder run
(skip 7 days → skip 30 days and ask the user once → retire). Never re-fingerprint a source to
make a zero look genuine, and never answer a block by changing what the client claims to be.

**"The site is blocking us" — but the site is fine.**
*Your own sandbox's refusal read as a remote block.* Four refusal signatures are four different
facts, never to be collapsed: the harness denied the tool call · the OS sandbox denied the
syscall · the OS returned `EPERM` · the remote host returned an HTTP status. Only the fourth is
about the site. Diagnose over HTTP only — **never DNS, ping or raw sockets**, which are blocked
in sandboxes, where the resulting timeout reads as "the site is down" for a host that returns
200. Pair every target with `https://example.com` as a control in the same call. **Never strip
`HTTP_PROXY`/`HTTPS_PROXY` "for a clean test"**: in a sandbox that forces a local proxy, removing
it manufactures `Could not resolve host` and you will diagnose an outage that does not exist. A
source unreachable from a cloud egress may be perfectly reachable from the user's own machine —
probe results are stored with the egress class they were measured in, and a worker whose class
differs re-probes rather than trusting the record.
