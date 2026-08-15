#!/bin/sh
set -eu
umask 077

project_dir=${PROJECT_DIR:-"$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"}
backup_dir=${BACKUP_DIR:-"$project_dir/backups"}
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
output="$backup_dir/sublet-tracker-$timestamp.dump.age"
mkdir -p "$backup_dir"

test -f "$project_dir/.env" || { echo "missing .env" >&2; exit 1; }

# Read only the few non-Compose settings this script needs. Do not source the
# .env file: shell evaluation would turn a compromised/malformed secret file
# into arbitrary code execution. Compose remains the source of database env.
read_env_value() {
  key=$1
  awk -v key="$key" '
    $0 !~ /^[[:space:]]*#/ && $0 ~ /^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=/ {
      line=$0
      sub(/^[[:space:]]*/, "", line)
      name=line
      sub(/=.*/, "", name)
      if (name == key) { sub(/^[^=]*=/, "", line); print line; exit }
    }
  ' "$project_dir/.env"
}

backup_age_recipient=$(read_env_value BACKUP_AGE_RECIPIENT)
backup_rclone_remote=$(read_env_value BACKUP_RCLONE_REMOTE)
backup_retention_days=$(read_env_value BACKUP_RETENTION_DAYS)
test -n "$backup_age_recipient" || { echo "BACKUP_AGE_RECIPIENT is required" >&2; exit 1; }
command -v age >/dev/null 2>&1 || { echo "age is required" >&2; exit 1; }

cd "$project_dir"
docker compose --env-file .env up --detach --wait db

# pg_dump is executed inside the private database container. The dump is
# encrypted as a stream; no unencrypted SQL/custom dump is written to disk.
docker compose --env-file "$project_dir/.env" exec --no-TTY db \
  sh -c 'exec pg_dump --format=custom --no-owner --no-privileges -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  | age --encrypt --recipient "$backup_age_recipient" --output "$output"

# Upload only after encryption. rclone credentials belong in its host config,
# not in this repository or .env. An empty remote intentionally skips upload.
if [ -n "$backup_rclone_remote" ]; then
  command -v rclone >/dev/null 2>&1 || { echo "rclone required for configured upload" >&2; exit 1; }
  rclone copyto "$output" "$backup_rclone_remote/$(basename "$output")" --immutable
fi

# Local retention is a convenience; off-host retention is controlled by the
# remote's lifecycle policy. Keep files only for the configured number of days.
find "$backup_dir" -type f -name 'sublet-tracker-*.dump.age' \
  -mtime "+${backup_retention_days:-35}" -delete
printf '%s\n' "$output"
