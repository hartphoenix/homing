#!/bin/sh
set -eu
umask 077

# Run as the unprivileged deploy user from the checked-out release directory.
# Do not enable shell tracing: .env values and DATABASE_URL must never be logged.
project_dir=${PROJECT_DIR:-"$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"}
lock_file=${DEPLOY_LOCK_FILE:-"$project_dir/.deploy.lock"}

if ! command -v flock >/dev/null 2>&1; then
  echo "flock (util-linux) is required" >&2
  exit 1
fi

exec 9>"$lock_file"
flock -n 9 || { echo "another deployment is already running" >&2; exit 75; }
cd "$project_dir"

test -f .env || { echo "missing $project_dir/.env (copy .env.example and fill secrets)" >&2; exit 1; }
docker compose --env-file .env config --quiet
docker compose --env-file .env build --pull web
# --no-deps is used for the one-shot web containers below, so explicitly wait
# here rather than assuming the database process is accepting connections.
docker compose --env-file .env up --detach --wait db
docker compose --env-file .env run --rm --no-deps web /opt/app/docker/migrate-with-lock.sh python manage.py migrate --noinput
docker compose --env-file .env run --rm --no-deps web /opt/app/docker/migrate-with-lock.sh python manage.py collectstatic --noinput
docker compose --env-file .env up --detach --remove-orphans web caddy
docker compose --env-file .env ps
