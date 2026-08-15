# Adversarial design review — 2026-08-15

The initial architecture was challenged by a separate adversarial design agent before implementation.

## Blocking findings resolved

| Finding | Resolution |
| --- | --- |
| Cron agents could not reliably discover projects or resume work | Added `me/projects`, monotonic project changes, exact run snapshots, atomic claims, leases, retries, cursors, and idempotent completion. |
| Adding an existing email created membership without consent | Every share creates a single-use expiring invitation requiring exact-email acceptance, with revoke/reissue and authority recheck. |
| User-wide agent tokens could outlive intended access | Tokens are expiring and revocable. They discover the user's current and future portfolio by design, while effective authority still intersects with each current membership role. |
| Object authorization was a convention | Central project policy/services, uniform 404/403 rules, transactional checks, an endpoint role matrix, and cross-project negative tests are required. |

## Additional accepted changes

- Opaque monotonic sync cursors and tombstones replace timestamp polling.
- Source identity, URL fallback, provenance, field-update policy, collision behavior, and per-item bulk idempotency are explicit.
- Prompt row locking/revision conflicts and lead ETags prevent silent lost updates.
- Threaded plain-text comments have author edits, owner moderation, soft deletion, limits, and change events.
- `/interested` defaults to the current user; group interest is explicit.
- Agent-token identity appears in audits and search runs.
- Signup, password change/token invalidation, deactivation, privacy projections, shared-trash reasons, restore behavior, JSON limits, URL validation, and prompt-injection guidance are defined.
- Deployment adds migration locking, network isolation, host hardening, encrypted off-host backups, restore drills, and alert hooks.
- Legacy browser state has an explicit import path.

## Product decisions

- Invitations always require acceptance.
- Agents may exchange email/password for a token; long-running agents should use a revocable user-wide token from **Equip an agent** instead of storing a password.
- Owners/editors edit criteria. Viewers read, express interest, and comment.
- Agents write comments only with `comments:write`.
- September 2026 is imported project data, not application behavior.

Live deployment still needs the Hetzner SSH target, domain/DNS, and desired public-registration policy. None blocks implementation or deployment packaging.
