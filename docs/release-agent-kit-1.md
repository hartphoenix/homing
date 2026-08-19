# Release record — agent kit v1

| | |
|---|---|
| Deployed commit | `8a2f97a` |
| Rollback target | `9c2dd79` (the commit production ran before this deploy) |
| Artifact | `/agent/pkg/homing-agent-kit-1.zip`, 17 files |
| Artifact digest | `sha256:3e69d4612cc0ab8fce732b7ea49dc367979429c759c72c8ea9bcc161d081605f` |
| Deployed | 2026-08-19, to `homing.hartphoenix.com` from `/opt/homing` |
| Owner for post-deployment monitoring | Hart |

The digest is origin-dependent: `/agent/` bakes the serving origin into every
script, so an archive fetched from a different host has a different hash by
design. The value above is the one production serves.

## What this release is

The "equip an agent" flow, rebuilt. A person copies one instruction naming a
single URL. Their agent fetches `/agent/`, probes its own environment, asks at
most three plain questions, pairs through an approval code so the person never
handles an access key, designs and probes listing sources for their own locale,
and installs a separate lean `homing-check` skill on a schedule.

## Verified in production after deploy

- `POST /api/v1/agent-link` → 201 with a 6-character code and a working
  `verification_uri_complete`. It returned **403** before this deploy: CSRF was
  rejecting every API write, so pairing and all lead writes were impossible.
- `/agent/`, `/agent/pkg/manifest.json`, the archive, `SKILL.md`, references and
  scripts all 200 with no session cookie and correct content types.
- No `__HOMING_ORIGIN__` placeholder survives in any served file; the real origin
  is present in its place.
- `/agent-setup/SKILL.md` → 301 to `/agent/pkg/SKILL.md`.
- `/health/ready` → 200; web container healthy; no migrations pending.
- On a real Apple Silicon Mac, device-code pairing stored the issued key in the
  login Keychain, the installed client read it back, and the authenticated API
  self-test listed two projects without exposing the key.
- The same install registered `com.homing.check` with launchd. Its immediate
  launch wrote `last-run.json`; Homing was already account-paused, so the cycle
  stopped with the documented exit 3 before search or lead writes.
- The installed self-test passed 13/13 checks, including file modes, launchd
  registration, token-leak scan, authenticated API read, and proof that the
  scheduled model receives only `JUDGE.md` as its prompt.

## Supported matrix

| | Status |
|---|---|
| Homing API + package serving | **Tested** — 260 automated tests, plus HTTP-layer tests over a real socket |
| Pairing, file secret store | **Tested** — full protocol coverage incl. secret-hygiene scans |
| POSIX generated scripts | **Tested** — `sh -n`, plus adversarial payloads executed against a stub |
| macOS launchd + Keychain | **Tested on real hardware** — pair/store/read-back, install, 13/13 self-test, registration and immediate fire |
| Linux systemd + `systemd-creds` | **Untested on real hardware** |
| Windows Task Scheduler + DPAPI | **Untested — no PowerShell interpreter has ever parsed these files** |
| Claude Code, Codex, Gemini, Cursor, Copilot, OpenCode | Implemented, untested end to end |
| Browser ChatGPT (no shell) | Reduced scope: no install, manual access-key path only |
| An agent that cannot fetch a URL | Unsupported |

## Known limitations

1. The real macOS install completed through pairing, Keychain storage, install,
   self-test and launchd registration. The agent-driven bootstrap path remains
   unproven, and the account-wide pause prevented a live search/lead upsert.
2. launchd is tested on real hardware. Linux systemd and Windows Task Scheduler
   remain untested on real hardware.
3. The macOS Keychain write/read path is tested. `secret-tool` and DPAPI writes
   remain untested on real hardware.
4. `shellcheck` is not in CI; only `sh -n`.
5. The native-ID change alters the id derived from a `guid` rule when the guid is
   a permalink. Nothing is installed yet, so no state needs migrating — this
   stops being free after the first real install.
6. The production archive is 204,659 of 262,144 permitted bytes (78%). Headroom
   is thinning.

## Rollback

```sh
cd /opt/homing
git checkout 9c2dd79
./docker/deploy.sh
./docker/smoke.sh https://homing.hartphoenix.com
```

Rolling back restores the pre-deploy state, in which pairing 403s. It is a
rollback to "the kit does not work", not to a working older kit. Prefer fixing
forward unless the deploy itself broke something that previously worked.

No user data or credentials are touched by a rollback: no migrations ran in this
deploy, and agent tokens live in the database independently of the served kit.
