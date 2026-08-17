# pairing.md — getting access to the user's Homing account

Load this in Phase 2.

Pairing exists so the user never types, pastes, or sees an access key, and so no key ever
enters this conversation. The user clicks a link, checks that a six-character code matches, and
presses Approve. That is the whole job you are asking of them.

**Two values are credentials: the `device_code` and the token.** Neither may appear in this
transcript, in argv, in a log, or in any file you later read. The `user_code` is not a secret —
you must show it.

## Path A — a shell is present (default)

Run the two calls from inside `bin/homing.py`, never from your own fetch tool, so the
`device_code` and the token stay out of your context. On macOS and Windows the store write
must happen in a process the **human** started, so write the script and print one line for
them to run.

1. `python3 scripts/homing.py pair-request --label "<agent label>" --note "<environment note>"
   --cadence <minutes>` — makes call 1 below and writes `<state>/pair-request.json` containing
   only `{user_code, verification_uri_complete, expires_at}`. Read that file.
2. Show the user the code and the link (wording below).
3. Tell the user to run exactly one line: `sh <config>/bin/connect.sh` (or
   `pwsh -File <config>\bin\connect.ps1`). That script polls call 2, writes the token straight
   into the secret store, verifies by status code, and writes `<state>/pair-result.json`
   containing `{ok, http, scopes, expires_at}` — **no token**. Read that file.

## The two calls

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

`agent_label` ≤120 chars, `environment_note` ≤200. Both are shown to the user on the approval
card, so write them in plain words — they are how the user recognises you.

**Call 2 — poll for the token.** Unauthenticated. Body is `{"device_code": "..."}`.

```
POST __HOMING_ORIGIN__/api/v1/agent-link/token
200 {"token": "...", "expires_at": "...", "scopes": [...]}      ← exactly once, ever
400 {"error": {"code": "...", "message": "...", "request_id": "..."}}
```

| `error.code` | Do |
|---|---|
| `authorization_pending` | Keep waiting. Poll again after `interval` seconds. |
| `slow_down` | Add 5 seconds to `interval`, then keep waiting. Never shorten it again. |
| `access_denied` | Stop. Tell the user, do not retry, do not request a new code. |
| `expired_token` | Start over from call 1, **once**. A second expiry means stop and ask the user what happened. |

Never poll faster than `interval`. Give up after `expires_in` seconds and tell the user the
request expired.

## What to say while they approve

> Open this and press Approve — it should be showing the same code I am: **7K4M2Q**
> __HOMING_ORIGIN__/link/?code=7K4M2Q

Tell them what they will see: a card naming this assistant, the code, what it will be able to do
(see their searches, add and update places, add comments) and what it cannot (change their
password, invite people, see payment or login details), and Approve / Deny buttons. Then say
plainly: **if the code on that page is not the code I just showed you, press Deny.** That match
is the only thing stopping them from approving somebody else's assistant.

## Storing the token — exact commands

The token goes from the poll response into the store on stdin, in one process. Never argv (it is
visible in the process list), never a temp file, never a variable you print, never a here-doc in
this conversation.

| Platform | Store |
|---|---|
| macOS | `printf '%s\n%s\n' "$T" "$T" \| security add-generic-password -U -a "$USER" -s homing-api-token -l 'Homing API token' -w` — `-w` last means prompt mode, and it asks **twice**, hence the doubled value. Read it back later with `/usr/bin/security` only; a language keyring library stamps a different partition list and causes an un-dismissable prompt loop. |
| Linux desktop | `printf '%s' "$T" \| secret-tool store --label='Homing API token' service homing account api-token` — no trailing newline; `secret-tool` reads to EOF and a `\n` becomes part of the secret. Not for headless: without a session bus it fails under the scheduler while working in the developer's SSH session. |
| Linux headless | `systemd-creds` (`LoadCredential=` in the unit), else the file fallback below. |
| Windows | Credential Manager if `Get-Command New-StoredCredential` already exists; otherwise DPAPI: `ConvertTo-SecureString $t -AsPlainText -Force \| ConvertFrom-SecureString \| Set-Content "$env:LOCALAPPDATA\Homing\token.dpapi" -Encoding ascii -NoNewline`, then `icacls <file> /inheritance:r /grant:r "$($env:USERNAME):(R,W)"`. On the manual path read it with `Read-Host -AsSecureString` so it never reaches PSReadLine history. DPAPI is keyed to this user on this machine and is **unavailable under an S4U scheduled task** — see `environments.md`. Never `cmdkey /pass:` (argv, and it cannot read back). |
| Fallback | `( umask 077; mkdir -p <config>; printf '%s\n' "$T" > <config>/token )`. `umask` **first** — `mkdir` then `chmod` leaves a real world-readable window. Raw token only, no `KEY=` prefix. |

Then, in every case:

- Verify by **HTTP status code alone**: `GET __HOMING_ORIGIN__/api/v1/me/projects` → 200. Never
  read the value back to check it saved.
- Confirm the config directory's **realpath** is not inside iCloud, Dropbox, OneDrive,
  Syncthing, or a synced Documents folder. This is the most common leak by a wide margin.
- The token never goes in the launchd plist, systemd unit, or scheduled-task definition —
  `launchctl print`, `systemctl show`, and `schtasks /query /v` all print those in cleartext.

## Path B — manual fallback

Only when you cannot make outbound POSTs at all. The user opens Homing, creates an access key in
the web UI, and pastes it into the store-writing prompt themselves.

Say the true sentence before they start: **"To do it this way I'll have to see your access key,
and it will pass through your clipboard and possibly this chat. If you'd rather not, we can stop
here."** This is the second choice, and you say so.

If they proceed, treat the key as compromised at birth: a separate key for this installation
only, labelled so Homing shows it was exposed. Never ask for their Homing password and never
call `POST /auth/token`.

## Later, at run time

| Status | Do |
|---|---|
| `401` | The key stopped working. Stop all writes. Do not retry, do not loop, do not prompt, do not rotate. After two consecutive 401s disable the timer and send one notification, ever: "Homing needs you to reconnect." |
| `403` | A permission problem, not an expiry. Never rotate and never re-pair. Record which call and which project, and report it. Trash, restore, and delete are **expected** to 403 — that is the design, not a fault. |

Rotation, when the user asks for it: create the new key, store it, verify with a `GET`, and only
then revoke the old one. Never revoke first. The connect script is the rotation script.
