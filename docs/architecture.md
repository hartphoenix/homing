# Architecture

Status: accepted after adversarial review on 2026-08-15. Review resolutions are recorded in `design-review.md`.

## Product boundary and system shape

This is a self-hosted collaborative research tracker. Humans use a low-noise web interface and search agents use a versioned REST API under the same user identity. The app stores search intent, continuity, leads, per-user interest, shared comments, and shared project trash. Scraping and cron scheduling stay external.

- Django 5.2 LTS monolith on Python 3.13 with PostgreSQL 17.
- Django templates and small vanilla-JavaScript enhancements; no SPA or Node build chain.
- Gunicorn serves HTML and `/api/v1/`; WhiteNoise serves versioned assets.
- Docker Compose runs `web`, `db`, and `caddy` on one Hetzner host.
- Caddy alone publishes ports 80/443, manages TLS, and proxies to Gunicorn. PostgreSQL and Docker Engine are never internet-exposed.

## Identity and authentication

- A custom Django user uses normalized unique email as the login identifier.
- Passwords use Argon2 with Django-compatible fallback hashers. Browser authentication uses secure HTTP-only SameSite=Lax sessions and CSRF protection.
- Agents get a random opaque bearer token either by device-code pairing (below, the intended path) or from a token a user creates by hand in the authenticated UI. Only SHA-256 token digests are stored.
- Agent tokens have explicit scopes and optional project restrictions. Scopes are `profile:read`, `projects:read`, `prompts:read`, `leads:read`, `leads:write`, `leads:destroy`, `comments:read`, `comments:write`, `interest:read`, `interest:write`, and `runs:write`. Tokens can never manage users, membership, invitations, or tokens.
- `leads:destroy` covers trashing and restoring a lead (including batch `trash`/`restore`) and is kept separate from `leads:write` on purpose: additive writes are reversible, trash/restore are the two verbs that undo a human's decision. A token minted by device-code pairing (below) always gets every scope except `leads:destroy`; only a token a human creates by hand in the web UI can carry it.
- `POST /api/v1/auth/token` (password exchange) still exists but is gated behind `ALLOW_PASSWORD_TOKEN_EXCHANGE`, default off; the route 404s, not 401s, when disabled. It is documented as legacy — the intended way for an agent to get a token is device-code pairing.
- Password-exchange tokens require an allowed-project list, default to read plus lead/run write scopes, expire after 90 days, and show a blast-radius warning. Creation, rotation, revocation, expiry, and use are audited by non-secret token ID.
- **Device-code pairing** (`POST /api/v1/agent-link`, `POST /api/v1/agent-link/token`) is the primary way an agent gets a token, modeled on RFC 8628. Both endpoints are unauthenticated — the agent has no credential yet. The agent requests a link and gets back a `device_code` (its own secret, never shown to the person), a six-character `user_code` (Crockford base32, `I`/`L`/`O`/`U` excluded), and verification URIs; the person approves or denies at `/link/` (login required); the agent polls `agent-link/token` at a server-set interval and receives the token exactly once — the link is then `consumed` and a repeat poll fails. Poll responses distinguish `authorization_pending`, `slow_down`, `access_denied`, and `expired_token`. Neither the device code nor the token is ever written to an audit summary in the clear, and the person never sees or handles the token.
- Auth responses are generic and throttled by IP plus normalized email using a shared database/cache backend. Secrets and Authorization headers never enter URLs or logs.
- Public registration follows `ALLOW_PUBLIC_SIGNUP` and is disabled by default. A valid pending invitation permits only its recipient to register, choose a required nickname, and continue to acceptance. A bootstrap management command is the self-hosted recovery path.
- Existing active accounts can request a generic, throttled password-reset email. Reset tokens expire after 15 minutes and become invalid when the password changes.
- Password changes rotate the browser session and revoke agent tokens unless explicitly retained. Deactivation invalidates sessions and tokens immediately.

## Authorization and sharing

- Central policy/service functions enforce every project read and mutation. Object IDs never authorize access. Inaccessible objects uniformly return 404; an authenticated principal with insufficient role/scope on a known project receives 403.
- Legacy role labels are `owner`, `editor`, and `viewer`, but every collaborator has equal project-content and invitation authority. A token's effective authority is the intersection of membership, token scopes, and optional project restrictions.
- Any collaborator invites by normalized email. Every share creates a pending invitation—even for existing users—so a mistyped email cannot disclose a project without consent.
- Invitations are random, digest-stored, single-use, seven-day, revocable/reissuable tokens. Acceptance requires an authenticated account with exactly the normalized invited email. Recipients can register first when necessary.
- Invite responses do not reveal whether an email has an account. Acceptance transactionally rechecks that the inviter remains an active project collaborator. Any collaborator may invite; invite, accept, reject, revoke, expiry, and role changes are audited.
- A project always retains at least one owner; final-owner removal is transactionally rejected.

### Role matrix

- Owner: membership role administration, final-owner safeguards, and the same project-content authority as every collaborator.
- Editor/viewer: equal project-content authority, including metadata, prompt/criteria, leads, interest, trash/restore, and invitations. Role labels remain for compatibility and owner safeguards.
- Comment authors edit/delete their own comments; owners retain moderation authority.
- Agent: the user's role intersected with token scopes and project restrictions; never membership/invitation administration.

## Data model

### User-owned

- `User`: email, password hash, active/staff flags, timestamps.
- `Profile`: display name, timezone, bio/personal context, versioned structured details, and `agent_paused_until` (a person-set pause that a scheduled runtime must check first). Private by default.
- `SavedPrompt`: user-owned title, prompt, timestamps. Copyable into a project but not live-linked.
- `AgentToken`: user, digest, non-secret prefix, name, scopes, project restrictions, expected cadence and environment note (descriptive only, authorize nothing), whether the raw value was ever shown in a chat/clipboard flow, expiry/revocation/usage timestamps.
- `AgentLink`: one device-code pairing attempt — device-code digest, six-character user code, agent label, environment note, requested cadence, status (`pending`/`approved`/`denied`/`expired`/`consumed`), poll count, approver, issued token. Never stores either credential in the clear.

### Collaborative project

- `Project`: UUID, name, slug, description, current prompt, versioned structured criteria, status, creator, timestamps, prompt revision number, latest change sequence.
- `ProjectMembership`: project, user, role, joined timestamp; unique pair.
- `ProjectInvitation`: project, normalized email, role, inviter, token digest, expiry, accepted/revoked timestamps.
- `PromptRevision`: immutable revision number, prompt, criteria, editor, timestamp. Current prompt update and revision append share one locked transaction.
- `SearchRun`: project, user, authenticated token identity, descriptive agent label, exact prompt/criteria snapshot, status, lease owner/expiry, attempt count, input/output cursors, continuation JSON, result counts, summary, timestamps, idempotency key. Statuses: `queued`, `claimed`, `running`, `completed`, `failed`, `cancelled`.
- `Lead`: UUID, project, source identity, canonical/source URLs, title, summary, location, price display and optional amount/currency, availability, housing type, date confidence, park notes, attributes, verification notes, status, trash actor/time, creator, timestamps, revision. Legacy trash reasons are migrated to `LeadComment`.
- `LeadInterest`: lead, user, timestamp; unique pair. Unsetting deletes the row.
- `LeadComment`: lead, author, plain-text body, created/edited/deleted timestamps. Authors edit their own and owners may moderate. Agents write only with `comments:write`.
- `ProjectChange`: monotonic per-project sequence, event/object type and ID, compact payload or tombstone, actor, timestamp. This is the durable agent sync feed.
- `AuditEvent`: append-only project event with actor kind, user, token ID if any, request ID, and redacted bounded mutation summary.

JSON fields have versioned schemas, enum/depth/byte limits, and migration rules. Text is bounded, autoescaped, and never interpreted as agent instructions. Lead URLs accept only HTTP(S); the server never fetches them.

## Lead identity and lifecycle

- Primary identity is `(project, source, source_listing_id)`. Without a source ID, the server computes a conservative canonical URL hash, removing fragments and known tracking parameters without collapsing arbitrary query strings.
- Suspected identity collisions return 409 instead of merging. Fields retain compact provenance: human/token ID, search run, and observed timestamp.
- Bulk upsert updates only supplied fields, never clears omitted fields, and cannot overwrite newer human edits without the current ETag.
- Bulk requests allow 100 items, process each item transactionally, and return created/updated/unchanged/conflict/error results. Idempotency keys are scoped to token+endpoint for seven days; payload mismatch returns 409.
- Trash is project-shared, visible to members/scoped agents, reversible by collaborators, and accepts an optional comment. `DELETE` means trash; permanent deletion is absent from v1. Re-upserting a trashed lead conflicts and never restores silently. Batch trash/restore/interest actions validate every lead against the current project and commit atomically.
- Interest is per-user, survives trash and membership removal, and becomes visible again if membership returns. Trashed leads are excluded from interested views unless explicitly included.
- Comments, interest, and audit records remain when a lead is trashed.

## Agent continuity and synchronization

- `GET /api/v1/me/projects` lists every available project with role and latest change cursor, plus a top-level `agent_paused_until`; a scheduled runtime checks that first and exits untouched while it is set.
- `GET /api/v1/me/token` introspects the calling credential itself (scopes, expiry, last use, `agent_paused_until`) without a second credential, so a runtime can warn before it goes stale.
- `GET /api/v1/projects/{id}/changes?cursor=...` returns ordered prompt, criteria, lead, comment, interest, trash/restore, membership, and run changes plus tombstones.
- Cursors encode a monotonic sequence, not timestamps. Changes are retained 90 days; expired cursors return 410 and direct the agent to take a fresh snapshot.
- Search-run claiming is atomic with a five-minute renewable lease. An expired claim can be retried. Completion requires its claim token and idempotency key.
- V1 permits only one claimed/running run per project. The newest completed run is the default continuation point; a user can explicitly select another.
- Every run stores the exact prompt/criteria revision used and the authenticated token identity, making outcomes reproducible and attributable.
- A run's `continuation` is a closed schema (worker, protocol, per-lane status, deferred/ownership lists); an unknown top-level field is rejected rather than passed through, so a scheduled run's own prior output cannot become a channel for untrusted listing text to launder itself into trusted memory.
- `GET /projects/{id}/search-runs` and `GET /projects/{id}/trash` are cursor-paginated and always ordered newest-first; `search-runs` also accepts `agent_label_prefix` so a multi-worker deployment can find the latest run for one worker family.

## API conventions

- JSON lives under `/api/v1`; HTML routes are separate. Bearer tokens and authenticated sessions work. Unsafe session API calls require CSRF.
- UUIDs, UTC ISO-8601 timestamps, cursor pagination, explicit ordering, request IDs, and bounded payloads are universal.
- Project listing filters include `status=active|trashed`, `interested_by=me|any|user:{uuid}`, date confidence, housing type, and text query.
- `/interested` defaults to the authenticated user's pile. `any` is an explicit group view returning display names, not private profiles. `/trash` is project-shared.
- Lead writes use ETags and `If-Match`. Prompt writes use an expected revision under row lock. Stale writes return 409 with the current representation.
- Mutation, audit, and change-feed writes commit in the same transaction.
- Error envelope: `{ "error": { "code", "message", "fields", "request_id" } }`. Statuses: auth 401, insufficient role/scope 403, inaccessible object 404, conflicts 409, expired cursor 410, validation 422, throttling 429.

### Endpoint groups

- `/api/v1/auth/register`, `/api/v1/auth/token` (off by default), `/api/v1/auth/tokens`, `/api/v1/auth/tokens/{id}`
- `/api/v1/agent-link`, `/api/v1/agent-link/token` — unauthenticated device-code pairing
- `/api/v1/me`, `/api/v1/me/token`, `/api/v1/me/profile`, `/api/v1/me/saved-prompts`, `/api/v1/me/projects`
- `/api/v1/projects`, `/api/v1/projects/{id}`
- `/api/v1/projects/{id}/members`, `/api/v1/projects/{id}/invitations`, `/api/v1/invitations/{token}/accept`
- `/api/v1/projects/{id}/prompt`, `/api/v1/projects/{id}/prompt-revisions`, `/changes`
- `/api/v1/projects/{id}/search-runs`, `/{run_id}`, `/claim`, `/heartbeat`, `/complete`
- `/api/v1/projects/{id}/leads`, `/bulk-upsert`, `/{lead_id}`, `/interest`, `/comments`, `/comments/{comment_id}`
- `/api/v1/projects/{id}/interested`, `/trash`

The checked-in API contract defines schemas and a method-by-method role/scope matrix. Required negative tests cover cross-project IDs, removed users, role changes, revoked tokens, stale invitations, stale ETags, and bulk collisions.

## The agent kit

A small Django app, `agentkit`, serves a self-installing package publicly at `/agent/` — no
login, no session, no cookie. Public delivery is deliberate: an agent arriving from a copied
instruction has no session yet, and a login wall would hand it an HTML login page instead of the
package it needs.

- Routes: `/agent/` (the bootstrap page, `index.md`), `/agent/pkg/VERSION`,
  `/agent/pkg/manifest.json`, `/agent/pkg/SKILL.md`, `/agent/pkg/references/<name>.md`,
  `/agent/pkg/scripts/<name>`, and `/agent/pkg/homing-agent-kit-<version>.zip`. Every route is
  anonymous, `require_safe` (GET/HEAD only), and cacheable (`Cache-Control: public`, an ETag, and
  `If-None-Match` → `304`). None of them sets `Content-Disposition` or `Vary: Cookie`, and an
  unknown or path-traversal-shaped name 404s — the route table matches package paths by exact
  string, so nothing is ever built from request input.
- The package source of truth is the `agentkit/package/` tree in the repository. The manifest and
  the zip archive are **computed in memory per request, from that tree, substituting one
  placeholder** (`__HOMING_ORIGIN__` → the deployment's public origin) into every file's bytes.
  There is no committed build artifact, so a manifest digest can never disagree with what the
  file routes actually serve. `python manage.py build_agentkit` validates the package (required
  files present, no file over the size ceiling, no hardcoded production host, no token-shaped
  string, every manifest path reachable under `/agent/pkg/`, the placeholder fully substituted)
  and prints the manifest it would serve.
- The package itself is `homing-agent-kit`: an `index.md` fetch ladder (byte-exact zip+sha256 with
  a shell, file-by-file without one, structural verification only for a fetcher that paraphrases
  pages to markdown); a Claude Skill `SKILL.md` (`homing-setup`) that probes the environment,
  pairs the account, designs and probes listing sources, fits the environment (isolation rung,
  scheduler, secret store), asks at most three questions, generates the runtime, and verifies it
  end to end before reporting back in plain language; and reference files loaded one phase at a
  time rather than all at once.
- The old login-walled `agent_skill_download` view and its `build_skill_markdown()` are gone.
  `/agent-setup/SKILL.md` (whatever a person may have already pasted into an assistant)
  permanently redirects to `/agent/pkg/SKILL.md`, so an old copy of the instruction still works.

### The script/model split that keeps the key out of model context

The installed runtime is split into two scripts that never share a process boundary's worth of
trust:

- `scripts/sources.py` fetches listing pages and extracts bounded records. It holds **no account
  key, no Homing access, and no model** — it is the half of the runtime that touches untrusted
  content, which is exactly why it holds no credential: a fully hijacked parse still has nothing
  to exfiltrate with. It honors `robots.txt`, fetches only hosts listed verbatim in `sources.json`,
  and writes `candidates.jsonl`: at most 40 records per project, each truncated to at most 600
  bytes serialized, with raw page HTML never leaving the script.
- A generated prompt, `JUDGE.md`, is the **only** thing a scheduled run feeds a model: the
  candidates file and the person's own project prompt, each wrapped in a per-run random delimiter
  so listing text cannot forge a closing tag and smuggle instructions past it. The model has no
  network access, no credentials, and no write tools — it scores and summarizes, nothing else.
  Measured on a real run, 4,902 KB of raw fetched HTML reduced to 24.4 KB actually reaching the
  model — roughly a 200x reduction — consistent with the ≤40-record, ≤600-byte-each ceiling
  `sources.py` enforces.
- `scripts/homing.py` is the only file that ever touches the account key: it reads it from the OS
  secret store at call time, sends it in exactly one header, follows zero redirects off the Homing
  origin, and has no trash/restore/delete/batch subcommand at all — there is no code path that
  could construct one, and a paired token lacks `leads:destroy` server-side regardless. It never
  sees page text.
- The installer skill (`homing-setup`, run once with the person present) and the runtime skill it
  generates (`homing-check`, invoked later by a scheduler, unattended) are deliberately separate
  files in separate directories. The generated runtime must not contain the key, its path, any
  Homing URL, discovery logic, an environment conditional, or a path back to the installer; the
  installer is never re-run on a schedule, and `homing-check`'s own selftest phase asserts that a
  scheduler-invoked run does not re-probe or load the installer.

## Privacy, audit, and untrusted content

- Profiles and saved prompts are private. Project serializers expose member UUID, nickname, email, role, and interest attribution; structured personal details never appear in project output.
- Comments are plain text, max 10,000 characters, autoescaped, soft-deleted, and included in the change feed. Authors edit/delete their own comments and owners may moderate; original moderated content is audit-restricted.
- Audit events are owner-visible, redacted/size-bounded, retained one year by default, and never contain passwords, token/invitation secrets, Authorization headers, or full profile JSON.
- Listing text, comments, prompts, and source pages are untrusted data. Agent documentation explicitly warns against treating embedded listing/comment content as instructions.

## Web UX

- Project switcher, overview, current prompt with history, lead board, trash, members/invitations, profile, and saved prompts.
- Active leads default to high-signal facts, interested-member names, comment count, and explicit unknowns. Filters persist in shareable URLs.
- Edits use focused dialogs/pages. Destructive actions are reversible, show actor/time, and may append an optional comment.
- Stale edit conflicts show the server version; failed saves retain input; partial imports are explicit; expired sessions redirect safely. Empty project/trash and pending-invite states explain the next action.
- Semantic accessible HTML, keyboard operation, visible focus, reduced motion, and responsive single-column cards.
- Existing data imports as one ongoing September 2026 project. A one-time legacy-state importer accepts old `localStorage` JSON and reports unknown IDs while mapping interest and shared trash.

## Operations

- Secrets live only in a root-readable host `.env`; placeholders live in `.env.example`.
- An unprivileged key-only SSH deploy user owns a fixed directory. Firewall permits SSH/80/443 and unattended host security updates are documented.
- A deploy lock allows one migration runner. Failed migrations prevent readiness. Health endpoints distinguish liveness/database readiness.
- Structured app logs go to stdout; Caddy logs rotate. Authorization is redacted at proxy and app layers.
- Nightly encrypted `pg_dump`, off-host copy, retention, and a tested restore procedure. Backup age, disk/database health, and container restarts have alert hooks.
- Images and Python dependencies are pinned. CI runs format/static checks, migration drift, unit/integration and authorization-negative tests, secret scanning, and container checks.
- Production config sets proxy trust, allowed hosts, secure cookies, security headers, request/page/batch limits, and database timeouts.

## Non-goals for v1

- Built-in scraping/browser automation or scheduler.
- Email delivery; invitation links are copied and delivered out of band.
- Social login, uploads, external full-text search, websockets, mobile apps, or routine hard deletion.

## Deployment inputs still needed

- Hetzner SSH host/user and desired deploy directory.
- Public domain with DNS pointed at the host and ports 80/443 open.
- Whether public signup stays enabled after initial users are created.
