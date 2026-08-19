# pairing.md — getting access to the user's Homing account

Load this in Phase 2.

Pairing exists so the user never types, pastes, or sees an access key, and so no key ever
enters this conversation. The user clicks a link, checks that a six-character code matches, and
presses Approve. That is the whole job you are asking of them.

**Two values are credentials: the `device_code` and the key.** Neither may appear in this
transcript, in argv, in a log, or in any file you later read. The `user_code` is not a secret —
you must show it.

Both credentials are handled inside `homing.py`, in one process, by two subcommands:
`pair-request` and `pair-poll`. Never make these two HTTP calls with your own fetch tool, and
never read the device-code file. If you do either, the code lands in your context and the
pairing is compromised.

## Path A — the person runs one line (default)

`install.py` generates the wrapper. You do not run the pairing yourself: you tell the person to
run one line, then read the two safe files it writes.

| Path | Mode | What it is |
|---|---|---|
| `<config>/connect.sh` (POSIX) / `<config>/connect.ps1` (Windows) | 0700 | the one line a person runs; holds no key |
| `<config>/bin/homing.py` | 0500 | the client both calls go through |
| `<config>/private/device-code.json` | 0600 | the device code, raw, alone. **Never read this.** Deleted when pairing ends |
| `<state>/pairing.json` | 0600 | safe metadata: `user_code`, links, `expires_at`, `interval`. Read this |
| `<state>/pairing-result.json` | 0600 | outcome only: `paired`, `error_class`, `expires_at`, `scopes`. Read this |
| `<config>/set-token.sh` / `.ps1` | 0700 | the fallback of Path B, when pairing cannot be used at all |

`<config>/private/` exists only for the pairing helper. It is named in no config file, no state
file and no skill file, and nothing else in the install points at it.

1. Tell the user to run exactly one line and leave the window open:
   `sh <config>/connect.sh` — or `powershell -NoProfile -ExecutionPolicy Bypass -File
   <config>\connect.ps1` on Windows.
2. The script runs `pair-request`, prints the code and the link itself, and waits.
3. Read `<state>/pairing.json` and say the wording below, so the code the user sees on screen,
   the code you say, and the code on the approval card are the same three codes.
4. When the script exits, read `<state>/pairing-result.json`. That file is the whole answer.

### The contract between the wrapper and the client

`connect.sh` / `connect.ps1` are thin: they own the private directory and the deletion trap,
and they call these two commands and nothing else. The exact lines, with the exact paths:

```
"$PY" "$HOMING_PY" pair-request --label <worker label> --note <machine name> \
      --cadence <minutes> --out "<state>/pairing.json" \
      --device-code-out "<config>/private/device-code.json"

"$PY" "$HOMING_PY" pair-poll --device-code-file "<config>/private/device-code.json" \
      --store --result "<state>/pairing-result.json"
```

Rules the wrapper must keep:

- `umask 077` before creating anything, and `<config>/private/` at 0700. Never create wide and
  narrow afterwards.
- `trap 'rm -f "$DEVICE_CODE"' EXIT INT TERM HUP` (PowerShell: `finally { Remove-Item -Force }`).
  `pair-poll` deletes the file itself on every ending, including Ctrl-C; the trap covers the
  window between the two commands.
- **Export the same key-store environment the runtime uses** — `HOMING_TOKEN_STORE`, and
  `HOMING_TOKEN_FILE` or `HOMING_KEYCHAIN_SERVICE` — *before* calling `pair-poll`. `--store`
  writes to whichever store those name (defaulting to this platform's), and `run.sh` reads from
  whatever it exports. If the two disagree the pairing looks fine and every scheduled run then
  fails with exit 78.
- No `eval`, no `sh -c`, no `Invoke-Expression`, and no key or device code in any argument.
- The wrapper reads only `user_code` / `verification_uri_complete` out of `--out`, and prints
  the result file's *path* on failure, never its contents plus a guess.

## The two subcommands

**`pair-request`** — makes call 1. Unauthenticated: no key exists yet.

```
homing.py pair-request --label <text> [--note <text>] [--cadence <minutes>]
                       --out <path> --device-code-out <path>
```

- `--label` ≤120 chars, `--note` ≤200. Both are shown to the user on the approval card, so
  write them in plain words — they are how the user recognises you.
- `--out` gets 0600 JSON with exactly five keys: `user_code`, `verification_uri`,
  `verification_uri_complete`, `expires_at`, `interval`. The same object, plus `"ok": true`,
  is the only thing printed on stdout.
- `--device-code-out` gets 0600 with the raw device code and nothing else. This path belongs
  outside every directory you read.

**`pair-poll`** — makes call 2 until it resolves. Owns the whole private exchange.

```
homing.py pair-poll --device-code-file <path> [--store] [--result <path>]
                    [--timeout <seconds>] [--interval <seconds>]
```

- Reads the device code from the file, never from an argument.
- Polls every `--interval` seconds (default 5), adds 5 seconds on `slow_down` and never
  shortens again, gives up at `--timeout` (default 600, the server's `expires_in`).
- `--store` writes the key **straight into the key store on a pipe**, then verifies it with one
  authenticated `GET __HOMING_ORIGIN__/api/v1/me/token` and reports only whether it answered.
  Without `--store` the key is discarded unused and `error_class` is `not_stored`.
- Deletes the device-code file on success, denial, expiry, timeout, network failure and Ctrl-C.
- `--result` gets 0600 JSON with exactly four keys: `paired`, `error_class`, `expires_at`,
  `scopes`. No key, no device code, no response body.

### Exit codes and `error_class`

| Exit | `error_class` | What happened, and what to do |
|---|---|---|
| 0 | `null` | Paired, stored, verified. Nothing left to do. |
| 0 | `not_stored` | Approved, but `--store` was not passed, so the key is gone. Pair again with `--store`. |
| 77 | `access_denied` | Denied, replayed, or an unknown device code. Stop. Do not retry and do not request a new code. |
| 75 | `expired_token` | The request expired. Start over from `pair-request`, **once**. A second expiry means stop and ask the user what happened. |
| 75 | `timeout` | Nobody approved it in time. Same rule: one more attempt, then ask. |
| 75 | `rate_limited` | Too many pairing starts from this address. Wait; do not loop. |
| 69 | `malformed_response` | Homing answered something this client will not treat as a key. Report it; change nothing. |
| 78 | `store_write_failed` / `verify_failed` | The key never reached the store, or did not read back. The pairing is spent — pair again after fixing the store. |
| 78 | `no_device_code` | The device-code file is missing, empty, or readable by other users. Nothing was spent; run `pair-request` again. |
| 75 | `unavailable` | The network dropped mid-exchange. The device code is gone with it, so start from `pair-request`. |

## The two calls (for reference — you do not make them yourself)

**Call 1 — request a code.** Unauthenticated.

```
POST __HOMING_ORIGIN__/api/v1/agent-link
{"agent_label": "Claude on Hart's MacBook",
 "environment_note": "macOS laptop, runs while logged in",
 "requested_cadence_minutes": 1440}

201 {"device_code": "...", "user_code": "7K4M2Q",
     "verification_uri": "__HOMING_ORIGIN__/link/",
     "verification_uri_complete": "__HOMING_ORIGIN__/link/?code=7K4M2Q",
     "expires_in": 600, "interval": 5}
```

**Call 2 — poll for the key.** Unauthenticated. Body is `{"device_code": "..."}`.

```
POST __HOMING_ORIGIN__/api/v1/agent-link/token
200 {"token": "...", "expires_at": "...", "scopes": [...]}      ← exactly once, ever
400 {"error": {"code": "...", "message": "...", "request_id": "..."}}
```

| `error.code` | `pair-poll` does |
|---|---|
| `authorization_pending` | Waits `interval` seconds and polls again. |
| `slow_down` | Adds 5 seconds to `interval`, then keeps waiting. Never shortens it again. |
| `access_denied` | Stops at once. Exit 77. An unknown code answers this too — the server refuses to be an oracle. |
| `expired_token` | Stops. Exit 75. |

A `user_code` is six characters of Crockford base32 with I, L, O and U removed, so there is no
character the user can misread into another one.

## What to say while they approve

> Open this and press Approve — it should be showing the same code I am: **7K4M2Q**
> __HOMING_ORIGIN__/link/?code=7K4M2Q

Tell them what they will see: a card naming this assistant, the code, what it will be able to do
(see their searches, add and update places, add comments) and what it cannot (change their
password, invite people, see payment or login details), and Approve / Deny buttons. Then say
plainly: **if the code on that page is not the code I just showed you, press Deny.** That match
is the only thing stopping them from approving somebody else's assistant.

## Where the key ends up

`pair-poll --store` picks the store from `HOMING_TOKEN_STORE`, or this platform's default, and
writes with the value on stdin — never argv, never a temp file, never a variable it prints.

| `HOMING_TOKEN_STORE` | Store, and how it is written |
|---|---|
| `keychain` (macOS default) | `security add-generic-password -U -a "$USER" -s $HOMING_KEYCHAIN_SERVICE -l 'Homing API token' -w`, the value fed twice because `-w` last is prompt mode and prompt mode asks twice. Read back with `/usr/bin/security` only; a language keyring library stamps a different partition list and causes an un-dismissable prompt loop. |
| `secret-tool` (Linux desktop default) | `secret-tool store --label='Homing API token' service homing account api-token`, no trailing newline — `secret-tool` reads to EOF and a `\n` becomes part of the secret. Not for headless: without a session bus it fails under the scheduler while working in the developer's SSH session. |
| `dpapi` (Windows default) | `ConvertTo-SecureString` → `ConvertFrom-SecureString` → `$HOMING_TOKEN_FILE` (default `%LOCALAPPDATA%\Homing\token.dpapi`), then `icacls /inheritance:r /grant:r`. Keyed to this user on this machine and **unavailable under an S4U scheduled task** — see `environments.md`. Never `cmdkey /pass:` (argv, and it cannot read back). |
| `file` | `$HOMING_TOKEN_FILE`, else `$XDG_CONFIG_HOME/homing/token`. Created 0600 at `open()` under `umask 077`; the raw key only, no `KEY=` prefix. Linux headless should prefer `systemd-creds` with `LoadCredential=` in the unit, which `homing.py` reads through `$CREDENTIALS_DIRECTORY` — but `pair-poll` cannot write there, so a headless install pairs into `file` and the unit is pointed at it. |

Then, in every case:

- Verification is **status code alone** — `pair-poll` already did it, and it never reads the
  value back to check it saved.
- Confirm the config directory's **realpath** is not inside iCloud, Dropbox, OneDrive,
  Syncthing, or a synced Documents folder. This is the most common leak by a wide margin.
- The key never goes in the launchd plist, systemd unit, or scheduled-task definition —
  `launchctl print`, `systemctl show`, and `schtasks /query /v` all print those in cleartext.

## Path B — manual fallback

**This is the second choice and you say so.** Use it only when pairing genuinely cannot work
here: no outbound POST at all, or a store that only a person at the keyboard can write. The
user opens Homing, creates an access key in the web UI, and pastes it into
`<config>/set-token.sh` / `.ps1`, which reads it without echoing and puts it in the same store.

Say the true sentence before they start: **"To do it this way I'll have to see your access key,
and it will pass through your clipboard and possibly this chat. If you'd rather not, we can stop
here."**

If they proceed, treat the key as compromised at birth: a separate key for this installation
only, labelled so Homing shows it was exposed. Never ask for their Homing password and never
call `POST /auth/token`.

## Later, at run time

| Status | Do |
|---|---|
| `401` | The key stopped working. Stop all writes. Do not retry, do not loop, do not prompt, do not rotate. After two consecutive 401s disable the timer and send one notification, ever: "Homing needs you to reconnect." |
| `403` | A permission problem, not an expiry. Never rotate and never re-pair. Record which call and which project, and report it. Trash, restore, and delete are **expected** to 403 — that is the design, not a fault. |

Rotation, when the user asks for it: pair again with `connect.sh` — it writes the new key over
the old one in the store and verifies it — and only then revoke the old key in the web UI.
Never revoke first. The connect script is the rotation script.
