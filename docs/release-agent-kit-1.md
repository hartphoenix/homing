# Release record — agent kit v1

| | |
|---|---|
| Deployed commit | `f0f2328` |
| Rollback target | `9c2dd79` (the commit production ran before this deploy) |
| Artifact | `/agent/pkg/homing-agent-kit-1.zip`, 17 files |
| Artifact digest | `sha256:5beaa4e2a063d10779e4484be22a98b565427fd2550d253c1ebe0304dfc1377c` |
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

## Supported matrix

| | Status |
|---|---|
| Homing API + package serving | **Tested** — 246 automated tests, plus HTTP-layer tests over a real socket |
| Pairing, file secret store | **Tested** — full protocol coverage incl. secret-hygiene scans |
| POSIX generated scripts | **Tested** — `sh -n`, plus adversarial payloads executed against a stub |
| macOS launchd + Keychain | **Untested on real hardware** |
| Linux systemd + `systemd-creds` | **Untested on real hardware** |
| Windows Task Scheduler + DPAPI | **Untested — no PowerShell interpreter has ever parsed these files** |
| Claude Code, Codex, Gemini, Cursor, Copilot, OpenCode | Implemented, untested end to end |
| Browser ChatGPT (no shell) | Reduced scope: no install, manual access-key path only |
| An agent that cannot fetch a URL | Unsupported |

## Known limitations

1. No real agent has completed an install end to end on real hardware. The
   scripts have been driven directly; the agent-driven path is unproven.
2. No scheduler has ever been registered. Every test uses `scheduler.kind: none`.
3. The Keychain, `secret-tool` and DPAPI **write** paths have never run. All
   testing uses the file store.
4. `shellcheck` is not in CI; only `sh -n`.
5. The native-ID change alters the id derived from a `guid` rule when the guid is
   a permalink. Nothing is installed yet, so no state needs migrating — this
   stops being free after the first real install.
6. Archive is 202,943 of 262,144 permitted bytes (77%). Headroom is thinning.

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
