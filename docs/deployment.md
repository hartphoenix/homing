# Production deployment

This deployment targets one Hetzner VM. Caddy is the only public service; the
web container is reachable only on the private Docker network and PostgreSQL is
on a second internal network with no published port. Cron/search orchestration
stays outside the application (for example, Hermes calls the agent API).

## Inputs and threat model

Before provisioning, supply:

- a Hetzner Ubuntu/Debian VM with a fixed public IPv4 (and IPv6 if used);
- a DNS `A`/`AAAA` record for the eventual `APP_DOMAIN`;
- an SSH deploy user, key-only access, and a fixed directory such as
  `/srv/sublet-tracker`;
- an age encryption identity held off-host for restore, and its public recipient
  for backups; and
- an off-host rclone remote with credentials readable only by the backup user.

The VM firewall should permit TCP 22 only from the operator's IP(s), and TCP
80/443 (plus UDP 443 for HTTP/3) from the Internet. Do not expose 5432,
8000, Docker's socket, or Caddy's admin API. Enable unattended security updates,
time synchronization, disk-usage alerts, and Hetzner snapshots as a secondary
recovery mechanism. A snapshot is not a substitute for an encrypted logical
backup.

## First install

Install Docker Engine and the Compose v2 plugin from their vendor repository,
`git`, `flock` (util-linux), `age`, and `rclone`. Keep Docker's socket root-only.
Create a dedicated deploy user and directory; grant that user only the Docker
permission required by the host's operating policy. Do not run the application
containers as root. The image creates UID/GID 10001 (`app`), and Compose drops
all capabilities from web and Caddy except Caddy's `NET_BIND_SERVICE`.

Clone the release into the fixed directory and create the secret file:

```sh
sudo install -d -o deploy -g deploy -m 0750 /srv/sublet-tracker
sudo -u deploy git clone <repository-url> /srv/sublet-tracker
sudo -u deploy cp /srv/sublet-tracker/.env.example /srv/sublet-tracker/.env
sudo chmod 600 /srv/sublet-tracker/.env
sudoedit /srv/sublet-tracker/.env
```

Set a unique random `DJANGO_SECRET_KEY`, a unique random database password,
the real domain and HTTPS origin, and a valid `DATABASE_URL` whose password
matches `POSTGRES_PASSWORD`. Keep `ALLOW_PUBLIC_SIGNUP=false` after bootstrapping
the intended accounts. Never commit `.env`, paste it into chat, or include it
in an issue. `docker compose config` can render interpolated configuration, so
do not redirect that output to logs or tickets.

## Deploy and upgrade

DNS must resolve before the first start so Caddy can obtain a certificate. From
the deploy user's checkout:

```sh
cd /srv/sublet-tracker
git fetch --tags origin
git checkout <reviewed-release-tag-or-commit>
./docker/deploy.sh
./docker/smoke.sh https://sublets.example.com
```

`deploy.sh` uses a host `flock`, validates Compose interpolation, builds the
web image, waits for PostgreSQL, runs migrations under a PostgreSQL advisory
lock, collects static assets, and only then starts web/Caddy. A failed migration
stops the deployment; do not bypass it by deleting migration files. The web
health check is liveness-only; `/health/ready` must report database readiness
and is checked by the smoke script through Caddy.

Images are pinned to `python:3.13.7-slim-bookworm`, `postgres:17.5-alpine`,
`caddy:2.9.1-alpine`, and the application dependency ranges are constrained in
`pyproject.toml` (with CI's `pip-audit` check). Update image tags and
dependencies as one reviewed change, run CI, test a restore, then deploy. If a digest-pinned mirror
is required by policy, replace these tags with the digest recorded by the
release process.

Rollback means checking out the previous known-good application commit and
rebuilding. Migrations are forward-only unless a reviewed reverse migration is
available; restore the database only using the documented restore drill.

## Operations and observability

```sh
docker compose --env-file .env ps
docker compose --env-file .env logs --since=15m web caddy
./docker/smoke.sh https://sublets.example.com
```

Application logs are structured to stdout. Caddy emits JSON logs. Log shipping
must redact `Authorization`, cookies, passwords, token/invitation values,
`DATABASE_URL`, and full profile JSON. Never enable `set -x` around deployment,
backup, or restore scripts. Alert on failed health checks, repeated container
restarts, migration failures, backup age, upload failures, database disk growth,
and filesystem exhaustion.

The application enforces secure cookies, CSRF for session mutations, host/origin
checks, request/body/batch limits, and proxy trust. Only Caddy terminates TLS;
do not add a second public reverse proxy or publish Gunicorn directly.

## Backups and recovery

Run `docker/backup.sh` nightly from a host timer under the deploy user. It
streams a PostgreSQL custom-format dump through `age`, writes only the encrypted
artifact, uploads it to the configured off-host rclone remote, and prunes old
local files. See [backup-restore.md](backup-restore.md) for key handling,
retention, and the mandatory quarterly restore drill. Monitor the timestamp of
the newest remote artifact, not merely the host timer.

## Account/bootstrap checklist

The application does not email invitations or password resets in v1. Use its
documented management command from the host for the initial owner, then create
revocable, project-restricted agent tokens in the UI. For a cron agent, prefer
that token over a stored password. If password exchange is required, use a
dedicated account and the narrowest scopes/projects; rotate it after a suspected
leak. Password changes revoke agent tokens by default.
