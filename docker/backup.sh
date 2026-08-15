#!/bin/sh
set -eu
umask 077

project_dir=${PROJECT_DIR:-"$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"}
backup_dir=${BACKUP_DIR:-"$project_dir/backups"}
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
output="$backup_dir/sublet-tracker-$timestamp.dump.age"
mkdir -p "$backup_dir"

test -f "$project_dir/.env" || { echo "missing .env" >&2; exit 1; }
# Compose reads this same file, and loading it here gives pg_dump its database
# name/user without placing credentials in command-line output or source.
set -a
. "$project_dir/.env"
set +a
test -n "${BACKUP_AGE_RECIPIENT:-}" || { echo "BACKUP_AGE_RECIPIENT is required" >&2; exit 1; }
command -v age >/dev/null 2>&1 || { echo "age is required" >&2; exit 1; }

# pg_dump is executed inside the private database container. The dump is
# encrypted as a stream; no unencrypted SQL/custom dump is written to disk.
docker compose --env-file "$project_dir/.env" exec --no-TTY db \
  pg_dump --format=custom --no-owner --no-privileges \
  -U "${POSTGRES_USER:?POSTGRES_USER must be exported or loaded by compose}" \
  -d "${POSTGRES_DB:?POSTGRES_DB must be exported or loaded by compose}" \
  | age --encrypt --recipient "$BACKUP_AGE_RECIPIENT" --output "$output"

# Upload only after encryption. rclone credentials belong in its host config,
# not in this repository or .env. An empty remote intentionally skips upload.
if [ -n "${BACKUP_RCLONE_REMOTE:-}" ]; then
  command -v rclone >/dev/null 2>&1 || { echo "rclone required for configured upload" >&2; exit 1; }
  rclone copyto "$output" "$BACKUP_RCLONE_REMOTE/$(basename "$output")" --immutable
fi

# Local retention is a convenience; off-host retention is controlled by the
# remote's lifecycle policy. Keep files only for the configured number of days.
find "$backup_dir" -type f -name 'sublet-tracker-*.dump.age' \
  -mtime "+${BACKUP_RETENTION_DAYS:-35}" -delete
printf '%s\n' "$output"
