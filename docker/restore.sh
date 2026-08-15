#!/bin/sh
set -eu
umask 077

project_dir=${PROJECT_DIR:-"$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"}
backup=${1:-}
if [ -z "$backup" ] || [ ! -f "$backup" ]; then
  echo "usage: $0 /absolute/path/to/sublet-tracker-<timestamp>.dump.age" >&2
  exit 2
fi
if [ "${RESTORE_CONFIRM:-}" != "YES" ]; then
  echo "restore overwrites application data; set RESTORE_CONFIRM=YES to continue" >&2
  exit 1
fi
test -f "$project_dir/.env" || { echo "missing .env" >&2; exit 1; }
set -a
. "$project_dir/.env"
set +a
command -v age >/dev/null 2>&1 || { echo "age is required" >&2; exit 1; }
test -n "${AGE_IDENTITY_FILE:-}" || { echo "AGE_IDENTITY_FILE is required" >&2; exit 1; }
test -r "$AGE_IDENTITY_FILE" || { echo "identity file is not readable" >&2; exit 1; }

cd "$project_dir"
docker compose --env-file .env stop web caddy
age --decrypt --identity "$AGE_IDENTITY_FILE" "$backup" \
  | docker compose --env-file .env exec --no-TTY db \
      pg_restore --clean --if-exists --no-owner --no-privileges \
      -U "${POSTGRES_USER:?POSTGRES_USER must be exported or loaded by compose}" \
      -d "${POSTGRES_DB:?POSTGRES_DB must be exported or loaded by compose}"
docker compose --env-file .env run --rm --no-deps web /opt/app/docker/migrate-with-lock.sh python manage.py migrate --noinput
docker compose --env-file .env up --detach web caddy
