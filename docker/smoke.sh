#!/bin/sh
set -eu

base_url=${1:-"https://${APP_DOMAIN:?APP_DOMAIN must be set}"}
case "$base_url" in
  https://*|http://*) ;;
  *) echo "base URL must begin with http:// or https://" >&2; exit 2 ;;
esac

python - "$base_url" <<'PY'
import sys
import urllib.error
import urllib.request

base = sys.argv[1].rstrip("/")
for path in ("/health/live", "/health/ready"):
    try:
        with urllib.request.urlopen(base + path, timeout=10) as response:
            if response.status != 200:
                raise SystemExit(f"{path}: expected 200, got {response.status}")
    except urllib.error.HTTPError as error:
        raise SystemExit(f"{path}: HTTP {error.code}")
print("health checks passed")
PY

