#!/bin/sh
set -eu
umask 077

project_dir=${PROJECT_DIR:-"$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"}
backup=${1:-}
if [ -z "$backup" ] || [ ! -f "$backup" ]; then
  echo "usage: $0 /absolute/path/to/homing-<timestamp>.dump.age" >&2
  exit 2
fi
if [ "${RESTORE_CONFIRM:-}" != "YES" ]; then
  echo "restore overwrites application data; set RESTORE_CONFIRM=YES to continue" >&2
  exit 1
fi
test -f "$project_dir/.env" || { echo "missing .env" >&2; exit 1; }
command -v age >/dev/null 2>&1 || { echo "age is required" >&2; exit 1; }

# Avoid sourcing .env (which would evaluate shell syntax). This host-only
# restore secret is deliberately not passed into a container.
age_identity_file=${AGE_IDENTITY_FILE:-}
if [ -z "$age_identity_file" ]; then
  age_identity_file=$(awk '
    $0 !~ /^[[:space:]]*#/ && $0 ~ /^[[:space:]]*AGE_IDENTITY_FILE=/ {
      sub(/^[[:space:]]*AGE_IDENTITY_FILE=/, ""); print; exit
    }
  ' "$project_dir/.env")
fi
test -n "$age_identity_file" || { echo "AGE_IDENTITY_FILE is required in environment or .env" >&2; exit 1; }
test -r "$age_identity_file" || { echo "identity file is not readable" >&2; exit 1; }

cd "$project_dir"
docker compose --env-file .env stop web caddy
docker compose --env-file .env up --detach --wait db
age --decrypt --identity "$age_identity_file" "$backup" \
  | docker compose --env-file .env exec --no-TTY db \
      sh -c 'exec pg_restore --clean --if-exists --no-owner --no-privileges -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
docker compose --env-file .env run --rm --no-deps web /opt/app/docker/migrate-with-lock.sh python manage.py migrate --noinput
docker compose --env-file .env up --detach web caddy
