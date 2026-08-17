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
  `/opt/homing`;
- an age encryption identity held off-host for restore, and its public recipient
  for backups; and
- an off-host rclone remote with credentials readable only by the backup user.

For the current PhoenixBot target, create an `A` record for
`homing.hartphoenix.com` pointing to `204.168.138.83`. The apex domain remains
on GitHub Pages. UFW currently allows only SSH, so add TCP 80 and 443 before
starting Caddy; add UDP 443 only if HTTP/3 is desired. Re-check these facts at
deploy time rather than assuming this audit remains current.

Host audit on 2026-08-15: Docker 29.6.2 and Compose 5.3.1 are installed; ports
80/443 are unused; the root filesystem has about 18 GB free; and clock sync and
unattended upgrades are enabled. A dedicated `deploy` user does not yet exist,
and `age` and `rclone` still need installation. Existing PhoenixBot services
publish only loopback ports and do not require Homing to share their database.

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
sudo install -d -o deploy -g deploy -m 0750 /opt/homing
sudo -u deploy git clone <repository-url> /opt/homing
sudo -u deploy cp /opt/homing/.env.example /opt/homing/.env
sudo chmod 600 /opt/homing/.env
sudoedit /opt/homing/.env
```

Set a unique random `DJANGO_SECRET_KEY`, a unique random database password,
the real domain and HTTPS origin, and a valid `DATABASE_URL` whose password
matches `POSTGRES_PASSWORD`. Keep `ALLOW_PUBLIC_SIGNUP=false` unless open
registration is intentional. A valid pending invitation lets its exact-email
recipient create an account and continue to project acceptance even while
public signup is closed. Never commit `.env`,
paste it into chat, or include it in an issue. `docker compose config` can
render interpolated configuration, so do not redirect that output to logs or
tickets.

### Where secrets live

The production secrets belong only in `/opt/homing/.env` on the VM. The deploy
user should own that file and its mode must remain `0600`. Do not put the
Resend key, Django secret, database password, backup private key, or rendered
`DATABASE_URL` in the repository or GitHub Actions. The current repository has
CI but no deployment workflow, so GitHub does not need production secrets.

Generate the two application secrets on a trusted machine or on the VM:

```sh
openssl rand -base64 48
openssl rand -base64 36
```

Use the first output as `DJANGO_SECRET_KEY` and the second as
`POSTGRES_PASSWORD`. Put the same database password into the password portion
of `DATABASE_URL`. The complete production mail block in `/opt/homing/.env`
should be:

```dotenv
EMAIL_HOST=smtp.resend.com
EMAIL_PORT=587
EMAIL_HOST_USER=resend
EMAIL_HOST_PASSWORD=re_replace_with_the_resend_key
EMAIL_USE_TLS=true
EMAIL_USE_SSL=false
EMAIL_TIMEOUT=10
DEFAULT_FROM_EMAIL=Homing <notifications@homing.hartphoenix.com>
PASSWORD_RESET_TIMEOUT=900
```

`EMAIL_HOST_PASSWORD` is the only Resend secret. The other mail values are
configuration, not credentials. After editing, validate permissions without
printing the file:

```sh
sudo chown deploy:deploy /opt/homing/.env
sudo chmod 600 /opt/homing/.env
sudo stat -c '%U:%G %a %n' /opt/homing/.env
```

If deployment is automated later, prefer leaving `.env` on the host and give a
GitHub Environment named `production` only these deployment credentials:
`DEPLOY_SSH_HOST`, `DEPLOY_SSH_USER`, and `DEPLOY_SSH_PRIVATE_KEY`. A future
workflow must be written to consume those exact names before adding them.

### Transactional email: Resend and Porkbun

1. In Resend, open **Domains**, choose **Add domain**, and add
   `homing.hartphoenix.com`. Keep **Receiving** off; Homing only needs outbound
   transactional mail. Leave the default return path (`send`) and leave open
   and click tracking off for authentication links.
2. Resend will show an SPF TXT record, an SPF/bounce MX record, and a DKIM TXT
   record. Copy their type, name, value, and MX priority exactly. These values
   are generated for the account/region; do not substitute example selectors.
3. In Porkbun, open **Domain Management → hartphoenix.com → DNS → Add Record**.
   Create each Resend record. Porkbun's **Host** is relative to
   `hartphoenix.com`, so remove that final suffix from Resend's full name. For
   example, `send.homing.hartphoenix.com` becomes Host `send.homing`, and
   `resend._domainkey.homing.hartphoenix.com` becomes Host
   `resend._domainkey.homing`. Put Resend's destination/content into
   **Answer/Value**, set the exact priority on the MX record, and leave TTL at
   Porkbun's default.
4. Return to Resend and click **Verify DNS Records**. Verification is often
   quick but DNS can take up to 72 hours. If it fails, check that the full
   public record names and values match Resend exactly; Porkbun may require a
   trailing dot on a fully qualified MX destination if it otherwise appends
   the zone name.
5. After the domain is verified, open **API Keys → Create API Key**. Name it
   `homing-production`, choose **Sending access**, and restrict it to
   `homing.hartphoenix.com`. Copy the `re_...` value immediately—it is shown
   once—and store it as `EMAIL_HOST_PASSWORD` in `/opt/homing/.env`.
6. Deploy, invite a test address, and request a password reset for an existing
   test account. Check delivery and confirm the received headers report SPF,
   DKIM, and DMARC as passing.

DMARC is optional for Resend verification but recommended. Once SPF and DKIM
are verified, add a Porkbun TXT record with Host `_dmarc.homing`. Start with
`v=DMARC1; p=none;` as the Answer. If a real mailbox will receive aggregate
reports, use
`v=DMARC1; p=none; rua=mailto:dmarc-reports@hartphoenix.com;` instead. Monitor
all senders before moving to `p=quarantine` or `p=reject`.

The visible From address does not need a Porkbun mailbox for outbound mail.
Replies to `notifications@homing.hartphoenix.com` will not reach Gmail unless
you separately buy/configure receiving or forwarding; that is not required for
invitations or password resets.

### Staging and local previews

For local review, use the deterministic demo store rather than copying live
personal data:

```sh
.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed_demo_data
.venv/bin/python manage.py runserver
```

The demo users are Alex, Blair, and Casey and their documented local-only
password is `homing-demo-password`. Keep local email on the console backend. To
persist messages as files instead, launch with
`EMAIL_FILE_PATH=/tmp/homing-mailbox .venv/bin/python manage.py runserver`.
Do not put the production Resend key in a local environment file.

A public staging environment should use `staging.homing.hartphoenix.com`, a
separate checkout/Compose project, separate PostgreSQL volume, a different
`DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD`, and its own host-only `.env`.
Create a Porkbun A record with Host `staging` pointing to the VM. Keep staging
mail file-based until the environment is access-controlled and uses only dummy
accounts. Never copy the production database into staging without first
scrubbing email addresses, comments, tokens, invitations, and audit metadata.

## Deploy and upgrade

DNS must resolve before the first start so Caddy can obtain a certificate. From
the deploy user's checkout:

```sh
cd /opt/homing
git fetch --tags origin
git checkout <reviewed-release-tag-or-commit>
./docker/deploy.sh
./docker/smoke.sh https://homing.hartphoenix.com
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
./docker/smoke.sh https://homing.hartphoenix.com
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

Configure `EMAIL_HOST`, SMTP credentials, `DEFAULT_FROM_EMAIL`, and the
provider-required SPF/DKIM records before relying on invitations or password
reset. Without `EMAIL_HOST`, messages are written to the container console and
are not delivered. Use the documented management command from the host for the
initial owner, then create revocable user-wide agent tokens with **Equip an agent** in the UI. These tokens
discover every current and future project available to that user, while the
user's role in each project still limits operations. For a cron agent, prefer
this token over a stored password and revoke it after a suspected leak. Password
changes revoke agent tokens by default.
