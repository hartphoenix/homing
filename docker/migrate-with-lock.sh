#!/bin/sh
set -eu

# Hold a PostgreSQL advisory lock for the entire migration process. This makes
# two deploys on the same host (or two operator shells) serialize safely while
# remaining portable to a future multi-web replica deployment.
if [ "${1:-}" = "gunicorn" ]; then
  shift
  set -- gunicorn \
    --bind "${GUNICORN_BIND:-0.0.0.0:8000}" \
    --workers "${GUNICORN_WORKERS:-3}" \
    --threads "${GUNICORN_THREADS:-2}" \
    --timeout "${GUNICORN_TIMEOUT:-60}" \
    "${GUNICORN_APP_MODULE:-config.wsgi:application}" "$@"
fi

python - <<'PY'
import os
import subprocess
import sys

try:
    import psycopg
except ImportError as exc:  # pragma: no cover - image build catches this
    raise SystemExit("psycopg is required for migration locking") from exc

url = os.environ.get("DATABASE_URL")
if not url:
    raise SystemExit("DATABASE_URL is required")

# This constant identifies this application, not a secret. The lock is held
# only for migrations; the long-running web process starts after it is released.
lock_key = 0x5355424C45545452
with psycopg.connect(url, connect_timeout=10) as connection:
    connection.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
    connection.commit()
    try:
        completed = subprocess.run(
            [sys.executable, "manage.py", "migrate", "--noinput"], check=False
        )
    finally:
        connection.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
        connection.commit()
sys.exit(completed.returncode)
PY

exec "$@"
