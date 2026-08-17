#!/usr/bin/env python3
"""homing.py - the Homing API client for the homing-agent-kit runtime.

This is the only file in the kit that ever touches the account key. Run it.
Do not read it into a model's context, and do not reimplement it with curl.

    homing.py --help
    homing.py projects
    homing.py leads-upsert --project <uuid> --items-file leads.json

Design rules this file enforces mechanically (they are not advice):

  * The Homing origin is a module constant, substituted once at install time.
    No argument, config file, or piece of data can redirect a request.
  * The key is read from the OS secret store at call time, sent in exactly one
    header, and never placed in argv, an environment value, a URL, a log line,
    stdout, or an exception message.
  * Zero redirects are followed. A redirect off the Homing origin would carry
    the Authorization header with it, so it is a hard error instead.
  * Any response whose body or headers echo the key exits 65 without printing.
  * There is no trash, restore, delete, or batch subcommand, and no code path
    that could construct one. Paired keys also lack `leads:destroy` server-side.

Exit codes:
    0   success (also: `run-claim` deferred, which prints {"deferred": true})
   64   usage error, or a request shape this client refuses to build
   65   the response echoed the account key
   66   the origin tried to redirect a key-bearing request
   69   5xx after retries, or a bot-wall wearing a 429 costume
   70   a hard bound was violated (write budget, destructive call, huge body)
   73   an outbound payload failed the closed-schema check
   75   transient network failure - try again on the next run
   77   401/403 - stop all writes. Do not retry, do not loop, do not prompt.
   78   no key available from the secret store, or the kit was never installed
"""

import argparse
import hashlib
import json
import logging
import os
import platform
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- installed constants -----------------------------------------------------

ORIGIN = "__HOMING_ORIGIN__"  # replaced with the real origin by the installer
API_PREFIX = "/api/v1"
USER_AGENT = "HomingAgent/1.0 (+%s/about/agent; user-directed housing search for one person)" % ORIGIN

# --- hard bounds (BRIEF 4.5) -------------------------------------------------

MAX_BATCH_ITEMS = 100
MAX_WRITE_CALLS = 120
MAX_DESTRUCTIVE_CALLS = 0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
HEARTBEAT_MIN_INTERVAL = 210  # seconds; the write phase must exceed ~4 min first
CLAIM_BACKOFF = (5, 15, 45, 120)
PARK_MAX_FILES = 200
PARK_MAX_AGE_DAYS = 7
PARK_REKEY_AFTER_DAYS = 6  # idempotency keys are retained 7 days server-side

EXIT_OK = 0
EXIT_USAGE = 64
EXIT_TOKEN_ECHO = 65
EXIT_REDIRECT = 66
EXIT_UNAVAILABLE = 69
EXIT_BOUND = 70
EXIT_VALIDATION = 73
EXIT_TEMPFAIL = 75
EXIT_AUTH = 77
EXIT_CONFIG = 78

LOG = logging.getLogger("homing")


# --- redaction ---------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"Bearer\s+\S+", re.I),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]+"),
    re.compile(r"\bghp_[A-Za-z0-9]+"),
    re.compile(r"\bst_live_[A-Za-z0-9_\-]+"),
    re.compile(r'"claim_token"\s*:\s*"[^"]*"'),
]


class Redactor(logging.Filter):
    """Every log line goes through this. Nothing else writes to stderr."""

    def __init__(self):
        logging.Filter.__init__(self)
        self.literals = set()

    def add(self, value):
        if value and len(value) >= 8:
            self.literals.add(value)

    def scrub(self, text):
        for literal in self.literals:
            text = text.replace(literal, "<redacted>")
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub("<redacted>", text)
        return text

    def filter(self, record):
        record.msg = self.scrub(record.getMessage())
        record.args = ()
        return True


REDACTOR = Redactor()


def setup_logging(verbose):
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("homing: %(levelname)s %(message)s"))
    handler.addFilter(REDACTOR)
    LOG.addHandler(handler)
    LOG.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOG.propagate = False


def die(code, message):
    LOG.error("%s", message)
    sys.exit(code)


def emit(obj):
    """The single stdout channel. Redacted, one JSON object, newline terminated."""
    sys.stdout.write(REDACTOR.scrub(json.dumps(obj, sort_keys=True)) + "\n")
    sys.stdout.flush()


# --- the account key ---------------------------------------------------------
#
# There is deliberately no way to pass the key as an argument or as an
# environment *value*. Environment variables here name a store or a path only.


def _default_store():
    system = platform.system()
    if system == "Darwin":
        return "keychain"
    if system == "Windows":
        return "dpapi"
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return "secret-tool"
    return "file"


def _run_quiet(argv, timeout=20):
    """Run a helper without ever surfacing its stdout in an exception."""
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None, 127
    except subprocess.TimeoutExpired:
        # A locked login keychain parks `security` on a GUI prompt forever.
        return None, 62
    return proc.stdout, proc.returncode


def _token_from_keychain():
    service = os.environ.get("HOMING_KEYCHAIN_SERVICE", "homing-api-token")
    account = os.environ.get("HOMING_KEYCHAIN_ACCOUNT") or os.environ.get("USER") or ""
    argv = ["/usr/bin/security", "find-generic-password", "-s", service, "-w"]
    if account:
        argv[3:3] = ["-a", account]
    out, rc = _run_quiet(argv)
    if rc == 44:
        die(EXIT_CONFIG, "no key in the login keychain; run set-token.sh")
    if rc != 0 or not out:
        die(EXIT_CONFIG, "keychain unavailable (status %s)" % rc)
    return out.decode("utf-8", "replace").strip("\r\n")


def _token_from_secret_tool():
    out, rc = _run_quiet(
        ["secret-tool", "lookup", "service", "homing", "account", "api-token"]
    )
    if rc != 0 or not out:
        die(EXIT_CONFIG, "secret-tool returned nothing (status %s)" % rc)
    return out.decode("utf-8", "replace").strip("\r\n")


def _token_from_dpapi():
    path = os.environ.get("HOMING_TOKEN_FILE") or os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Homing", "token.dpapi"
    )
    script = (
        "$s = Get-Content -Path '%s' -Raw | ConvertTo-SecureString; "
        "[System.Net.NetworkCredential]::new('', $s).Password" % path.replace("'", "''")
    )
    out, rc = _run_quiet(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    )
    if rc != 0 or not out:
        die(EXIT_CONFIG, "DPAPI store unreadable (status %s)" % rc)
    return out.decode("utf-8", "replace").strip("\r\n")


def _token_file_candidates():
    explicit = os.environ.get("HOMING_TOKEN_FILE")
    if explicit:
        return [explicit]
    paths = []
    creds = os.environ.get("CREDENTIALS_DIRECTORY")  # systemd LoadCredential
    if creds:
        paths.append(os.path.join(creds, "homing-api-token"))
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    paths.append(os.path.join(xdg, "homing", "token"))
    return paths


def _token_from_file():
    for path in _token_file_candidates():
        if not os.path.isfile(path):
            continue
        mode = os.stat(path).st_mode
        if mode & 0o077 and os.name != "nt":
            die(EXIT_CONFIG, "key file %s is readable by other users; chmod 600 it" % path)
        with open(path, "rb") as handle:
            value = handle.read(4096).decode("utf-8", "replace").strip("\r\n")
        if value:
            return value
    die(EXIT_CONFIG, "no key file found; run set-token.sh")


_TOKEN_CACHE = []


def token():
    """Read the key from the secret store at call time. Never cached to disk."""
    if _TOKEN_CACHE:
        return _TOKEN_CACHE[0]
    store = os.environ.get("HOMING_TOKEN_STORE") or _default_store()
    readers = {
        "keychain": _token_from_keychain,
        "secret-tool": _token_from_secret_tool,
        "dpapi": _token_from_dpapi,
        "file": _token_from_file,
    }
    if store not in readers:
        die(EXIT_CONFIG, "unknown key store %r" % store)
    value = readers[store]()
    if not value:
        die(EXIT_CONFIG, "the %s store held an empty key" % store)
    REDACTOR.add(value)
    _TOKEN_CACHE.append(value)
    return value


# --- HTTP --------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError, which we turn into exit 66


_OPENER = urllib.request.build_opener(_NoRedirect)
_OPENER.addheaders = []

_BOT_HEADER_MARKERS = (
    "cf-mitigated",
    "x-amzn-waf-action",
    "x-px-blocked",
    "x-datadome",
    "x-iinfo",
)

_PATH_REFUSED = re.compile(r"://|@|[\r\n]")
_DESTRUCTIVE_PATH = re.compile(r"/trash|/restore|/leads/batch")


def _origin_parts():
    # Assembled at runtime so the installer's literal search-and-replace for the
    # placeholder cannot rewrite this guard along with the constant above.
    placeholder = "__" + "HOMING_ORIGIN" + "__"
    if placeholder in ORIGIN:
        die(EXIT_CONFIG, "this copy was never installed; the origin is still a placeholder")
    parts = urllib.parse.urlsplit(ORIGIN)
    if parts.scheme not in ("https", "http") or not parts.netloc:
        die(EXIT_CONFIG, "the installed origin is not a URL")
    host = parts.hostname or ""
    if parts.scheme != "https" and host not in ("localhost", "127.0.0.1", "::1"):
        die(EXIT_CONFIG, "the installed origin is not https")
    return parts


def _url(path):
    if not path.startswith("/") or path.startswith("//"):
        die(EXIT_USAGE, "refuse: path must be a bare absolute path")
    if _PATH_REFUSED.search(path):
        die(EXIT_USAGE, "refuse: path may not carry a host, credentials, or a newline")
    if _DESTRUCTIVE_PATH.search(path):
        die(EXIT_BOUND, "refuse: this client performs no destructive operations")
    origin = _origin_parts()
    url = "%s://%s%s%s" % (origin.scheme, origin.netloc, API_PREFIX, path)
    check = urllib.parse.urlsplit(url)
    if check.scheme != origin.scheme or check.netloc != origin.netloc:
        die(EXIT_USAGE, "refuse: assembled URL left the installed origin")
    return url


class Response(object):
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body
        self.json = {}
        if body:
            try:
                self.json = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self.json = {}

    def error_code(self):
        err = self.json.get("error") if isinstance(self.json, dict) else None
        if isinstance(err, dict):
            return str(err.get("code") or "")
        return ""

    def etag(self):
        return self.headers.get("ETag", "")


def _budget_dir(required):
    run_dir = os.environ.get("HOMING_RUN_DIR")
    if not run_dir:
        if required:
            die(EXIT_CONFIG, "HOMING_RUN_DIR is unset; the run wrapper must provide it")
        return None
    try:
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
    except OSError as exc:
        die(EXIT_CONFIG, "run directory unusable: %s" % exc.strerror)
    return run_dir


def _spend_write():
    run_dir = _budget_dir(required=True)
    path = os.path.join(run_dir, "budget.write")
    used = 0
    if os.path.exists(path):
        try:
            with open(path) as handle:
                used = int(handle.read().strip() or "0")
        except (ValueError, OSError):
            used = MAX_WRITE_CALLS  # unreadable budget fails closed
    used += 1
    if used > MAX_WRITE_CALLS:
        die(EXIT_BOUND, "write budget exhausted (%d calls); failing loudly" % MAX_WRITE_CALLS)
    with open(path, "w") as handle:
        handle.write(str(used))


def _check_no_echo(tok, response):
    if not tok:
        return
    needle = tok.encode("utf-8")
    if response.body and needle in response.body:
        die(EXIT_TOKEN_ECHO, "refuse: the response body echoed the account key")
    for value in response.headers.values():
        if tok in value:
            die(EXIT_TOKEN_ECHO, "refuse: a response header echoed the account key")


def _bot_wall(response):
    lowered = dict((k.lower(), (v or "").lower()) for k, v in response.headers.items())
    for marker in _BOT_HEADER_MARKERS:
        if marker in lowered:
            return True
    if "cloudflare" in lowered.get("server", "") and "cf-ray" in lowered:
        return True
    body = (response.body or b"")[:4096].lower()
    return b"just a moment" in body or b"/cdn-cgi/challenge-platform" in body


def request(method, path, payload=None, idempotency_key=None, if_match=None,
            retries=2, allow=()):
    """One HTTP call. Implements the BRIEF 4.4 error table.

    `allow` names status codes the caller will handle itself; everything else
    that is not 2xx ends the process with the documented exit code.
    """
    if method == "DELETE":
        die(EXIT_BOUND, "refuse: DELETE is not implemented and never will be")
    url = _url(path)
    tok = token()

    body = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if tok.encode("utf-8") in body:
            die(EXIT_TOKEN_ECHO, "refuse: the request body contains the account key")
    if method != "GET":
        _spend_write()

    headers = {
        "Authorization": "Bearer " + tok,  # the one and only place this appears
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    if if_match:
        headers["If-Match"] = if_match

    attempt = 0
    while True:
        attempt += 1
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with _OPENER.open(req, timeout=45) as raw:
                data = raw.read(MAX_RESPONSE_BYTES + 1)
                response = Response(raw.status, dict(raw.headers.items()), data)
        except urllib.error.HTTPError as exc:
            data = exc.read(MAX_RESPONSE_BYTES + 1) if exc.fp else b""
            response = Response(exc.code, dict(exc.headers.items()), data)
        except urllib.error.URLError as exc:
            if attempt > retries:
                die(EXIT_TEMPFAIL, "network failure reaching Homing: %s" % exc.reason)
            time.sleep(2 ** attempt)
            continue
        except OSError as exc:
            if attempt > retries:
                die(EXIT_TEMPFAIL, "network failure reaching Homing: %s" % exc)
            time.sleep(2 ** attempt)
            continue

        if len(response.body) > MAX_RESPONSE_BYTES:
            die(EXIT_BOUND, "refuse: response larger than %d bytes" % MAX_RESPONSE_BYTES)
        _check_no_echo(tok, response)

        status = response.status
        if 300 <= status < 400:
            die(EXIT_REDIRECT, "refuse: origin redirected a key-bearing request (%d)" % status)
        if 200 <= status < 300 or status in allow:
            return response

        if status == 401:
            die(EXIT_AUTH, "401 from Homing. Stopping. Homing needs you to reconnect.")
        if status == 403:
            die(EXIT_AUTH, "403 from Homing on %s %s - scope or role problem, not retryable"
                % (method, path))
        if status == 429:
            retry_after = response.headers.get("Retry-After", "").strip()
            if not retry_after and _bot_wall(response):
                die(EXIT_UNAVAILABLE, "429 with a bot-wall marker and no Retry-After: "
                                      "this is a block, not a rate limit. Not retrying.")
            if attempt > retries:
                die(EXIT_TEMPFAIL, "throttled by Homing; try again next run")
            delay = 30.0
            try:
                delay = min(float(retry_after), 120.0)
            except ValueError:
                pass
            LOG.info("429; honoring Retry-After %.0fs", delay)
            time.sleep(delay)
            continue
        if 500 <= status < 600:
            if attempt > retries:
                die(EXIT_UNAVAILABLE, "Homing returned %d after %d attempts" % (status, attempt))
            time.sleep(1 + 3 * attempt)
            continue

        die(EXIT_UNAVAILABLE, "unhandled %d (%s) from %s %s"
            % (status, response.error_code() or "-", method, path))


# --- small helpers -----------------------------------------------------------

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_CURSOR = re.compile(r"^[A-Za-z0-9_=:.|@+-]{1,256}$")
_LANE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}:[a-z0-9][a-z0-9-]{0,39}$")
_WORKER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URLISH = re.compile(r"\b(?:https?://|www\.)\S+", re.I)


def need_uuid(value, label):
    if not value or not _UUID.match(value):
        die(EXIT_USAGE, "%s must be a UUID" % label)
    return value


def clean_text(value, limit):
    text = _CONTROL.sub("", str(value)).replace("\r", " ")
    return text[:limit]


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def digest(*parts):
    joined = "\x1f".join(str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def idempotency_key(prefix, *parts):
    return ("homing-%s-%s" % (prefix, digest(*parts)[:40]))[:200]


def read_json_file(path, label):
    try:
        if path == "-":
            return json.loads(sys.stdin.read())
        with open(path, "rb") as handle:
            return json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        die(EXIT_USAGE, "cannot read %s (%s): %s" % (label, path, exc))


def write_private(path, payload):
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(payload, sort_keys=True))


# --- claim file (the claim token never reaches stdout, argv, or a log) -------


def claim_path(args):
    explicit = getattr(args, "claim_file", None)
    if explicit:
        return explicit
    run_dir = _budget_dir(required=True)
    return os.path.join(run_dir, "claim.json")


def load_claim(args, project_id, run_id):
    path = claim_path(args)
    if not os.path.exists(path):
        die(EXIT_CONFIG, "no claim on file; run-claim has not succeeded this run")
    with open(path) as handle:
        claim = json.load(handle)
    if claim.get("project_id") != project_id or claim.get("run_id") != run_id:
        die(EXIT_USAGE, "the stored claim is for a different run")
    REDACTOR.add(claim.get("claim_token", ""))
    return claim, path


# --- closed-schema validation ------------------------------------------------

LEAD_FIELDS = {
    "source": ("str", 120),
    "source_listing_id": ("str", 300),
    "url": ("url", 2000),
    "title": ("str", 500),
    "summary": ("str", 10000),
    "location": ("str", 500),
    "price_display": ("str", 200),
    "price_amount": ("num", None),
    "currency": ("str", 3),
    "availability": ("str", 200),
    "housing_type": ("str", 100),
    "date_confidence": ("enum", ("strong", "verify", "unknown")),
    "parks": ("str", 1000),
    "attributes": ("obj", 40),
    "verification_notes": ("str", 5000),
    "search_run_id": ("uuid", None),
    "observed_at": ("dt", None),
    "if_match": ("str", 200),
}
LEAD_REQUIRED = ("source", "url", "title")

RESULT_COUNT_KEYS = (
    "created", "updated", "unchanged", "conflicts", "trashed", "restored",
    "sources_ok", "sources_blocked", "suspected_injection", "urls_refused",
)
LANE_STATUS = (
    "ok", "empty", "blocked", "skipped",
    "skipped_needs_local", "skipped_needs_human", "error",
)
NEXT_ENUM = ("broaden_radius", "narrow_price", "next_page", "done")
BANNED_STATE_KEYS = ("next_query", "notes", "strategy", "learnings", "remember", "instructions")


def validate_lead(item):
    """Return (clean_item, problems). Omits rather than clears; truncates rather
    than rejects; drops the whole item only when identity or a URL is unusable."""
    problems = []
    if not isinstance(item, dict):
        return None, ["item is not an object"]
    unknown = sorted(set(item) - set(LEAD_FIELDS))
    if unknown:
        problems.append("unknown fields dropped: " + ",".join(unknown[:5]))
    clean = {}
    for name, (kind, limit) in LEAD_FIELDS.items():
        if name not in item:
            continue
        value = item[name]
        if value is None or value == "":
            continue  # never clear a populated field: omit instead
        if kind == "str":
            text = clean_text(value, limit)
            if not text:
                continue
            if len(str(value)) > limit:
                problems.append("%s truncated" % name)
            clean[name] = text
        elif kind == "url":
            text = clean_text(value, limit)
            parts = urllib.parse.urlsplit(text)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                return None, ["url is not http(s)"]
            clean[name] = text
        elif kind == "num":
            try:
                number = float(value)
            except (TypeError, ValueError):
                problems.append("price_amount dropped")
                continue
            if number < 0:
                problems.append("price_amount dropped")
                continue
            clean[name] = number
        elif kind == "enum":
            if value not in limit:
                problems.append("%s dropped (not in enum)" % name)
                continue
            clean[name] = value
        elif kind == "obj":
            if not isinstance(value, dict):
                problems.append("attributes dropped")
                continue
            trimmed = {}
            for key in sorted(value)[:limit]:
                trimmed[clean_text(key, 40)] = clean_text(value[key], 120)
            if trimmed:
                clean[name] = trimmed
        elif kind == "uuid":
            if not _UUID.match(str(value)):
                problems.append("%s dropped" % name)
                continue
            clean[name] = str(value)
        elif kind == "dt":
            if not _ISO.match(str(value)):
                problems.append("%s dropped" % name)
                continue
            clean[name] = str(value)
    for name in LEAD_REQUIRED:
        if not clean.get(name):
            return None, problems + ["missing required field %s" % name]
    return clean, problems


def validate_continuation(value):
    """BRIEF 7.4 closed schema. No free text, ever - an enum cannot carry a payload."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        die(EXIT_VALIDATION, "continuation must be an object")
    for banned in BANNED_STATE_KEYS:
        if banned in value:
            die(EXIT_VALIDATION,
                "continuation.%s is forbidden: persisted state carries no free text" % banned)
    out = {"protocol": 1}
    worker = str(value.get("worker", ""))
    if worker:
        if not _WORKER.match(worker):
            die(EXIT_VALIDATION, "continuation.worker is not a slug")
        out["worker"] = worker
    for key in ("lanes_owned", "needs_local", "needs_human"):
        lanes = value.get(key) or []
        if not isinstance(lanes, list):
            die(EXIT_VALIDATION, "continuation.%s must be a list" % key)
        for lane in lanes:
            if not isinstance(lane, str) or not _LANE.match(lane):
                die(EXIT_VALIDATION, "continuation.%s holds a non-lane value" % key)
        out[key] = lanes[:40]
    rows = value.get("lanes") or []
    if not isinstance(rows, list):
        die(EXIT_VALIDATION, "continuation.lanes must be a list")
    out["lanes"] = []
    for row in rows[:40]:
        if not isinstance(row, dict):
            die(EXIT_VALIDATION, "continuation.lanes holds a non-object")
        lane = str(row.get("lane", ""))
        status = str(row.get("status", ""))
        if not _LANE.match(lane) or status not in LANE_STATUS:
            die(EXIT_VALIDATION, "continuation.lanes entry failed validation")
        entry = {"lane": lane, "status": status}
        covered = str(row.get("covered_through", ""))
        if covered:
            if not _ISO.match(covered):
                die(EXIT_VALIDATION, "covered_through is not ISO-8601")
            entry["covered_through"] = covered
        for count in ("items_seen", "items_new"):
            if count in row:
                entry[count] = max(0, int(row[count]))
        out["lanes"].append(entry)
    out["deferred_batches"] = max(0, int(value.get("deferred_batches", 0) or 0))
    if "next" in value:
        if value["next"] not in NEXT_ENUM:
            die(EXIT_VALIDATION, "continuation.next must be one of %s" % (NEXT_ENUM,))
        out["next"] = value["next"]
    return out


def validate_result_counts(value):
    if not isinstance(value, dict):
        die(EXIT_VALIDATION, "result_counts must be an object")
    out = {}
    for key in RESULT_COUNT_KEYS:
        try:
            out[key] = max(0, int(value.get(key, 0) or 0))
        except (TypeError, ValueError):
            die(EXIT_VALIDATION, "result_counts.%s is not an integer" % key)
    if out["trashed"] or out["restored"]:
        die(EXIT_BOUND, "refuse: this client destroys nothing, so trashed/restored must be 0")
    return out


def validate_summary(value):
    """Summaries are read back by later runs, so they carry no links and no
    delimiter-shaped text. Stripping is mechanical; it does not judge content."""
    text = clean_text(value or "", 1000)
    text = _URLISH.sub("[link removed]", text)
    return text.replace("<", "").replace(">", "")


def validate_cursor(value, label="cursor"):
    """Outbound cursors: we built them, so a bad one is a bug. Hard fail."""
    if value in (None, ""):
        return ""
    text = str(value)
    if not _CURSOR.match(text):
        die(EXIT_VALIDATION, "%s failed the closed-schema check" % label)
    return text


def sanitize_cursor(value):
    """Inbound cursors: read from disk or from a response, so validate and
    fail closed to a fresh snapshot. Returns (cursor, state_reset)."""
    if value in (None, ""):
        return "", 0
    text = str(value).strip()
    if not _CURSOR.match(text):
        LOG.info("stored cursor failed validation; taking a fresh snapshot")
        return "", 1
    return text, 0


# --- read subcommands --------------------------------------------------------


def cmd_projects(args):
    response = request("GET", "/me/projects")
    body = response.json if isinstance(response.json, dict) else {}
    items = body.get("items") or []
    paused_until = str(body.get("agent_paused_until") or "")
    for project in items:  # tolerate a per-project echo of the same field
        value = project.get("agent_paused_until") if isinstance(project, dict) else None
        if value:
            paused_until = str(value)
    emit({"ok": True, "count": len(items), "paused": bool(paused_until),
          "paused_until": paused_until, "projects": items})


def cmd_project(args):
    project_id = need_uuid(args.project, "--project")
    response = request("GET", "/projects/%s" % project_id)
    emit({"ok": True, "project": response.json, "etag": response.etag()})


def cmd_prompt(args):
    project_id = need_uuid(args.project, "--project")
    response = request("GET", "/projects/%s/prompt" % project_id)
    emit({"ok": True, "prompt": response.json})


def cmd_changes(args):
    project_id = need_uuid(args.project, "--project")
    cursor, reset = sanitize_cursor(args.cursor)
    if args.cursor_file and os.path.exists(args.cursor_file) and not cursor:
        with open(args.cursor_file) as handle:
            cursor, extra = sanitize_cursor(handle.read())
            reset += extra
    limit = max(1, min(int(args.limit), 100))
    for _ in range(2):  # one retry, without the cursor
        query = {"limit": str(limit)}
        if cursor:
            query["cursor"] = cursor
        path = "/projects/%s/changes?%s" % (project_id, urllib.parse.urlencode(query))
        response = request("GET", path, allow=(410,))
        if response.status != 410 and response.error_code() != "cursor_expired":
            break
        LOG.info("cursor expired; taking a fresh snapshot")
        cursor, reset = "", reset + 1
    else:
        die(EXIT_UNAVAILABLE, "the change feed rejected even a fresh snapshot")
    body = response.json if isinstance(response.json, dict) else {}
    next_cursor, extra = sanitize_cursor(body.get("next_cursor"))
    reset += extra
    if args.cursor_file and next_cursor:
        os.makedirs(os.path.dirname(args.cursor_file) or ".", mode=0o700, exist_ok=True)
        with open(args.cursor_file, "w") as handle:
            handle.write(next_cursor)
    emit({"ok": True, "cursor_expired": bool(reset), "state_reset": reset,
          "next_cursor": next_cursor, "items": body.get("items") or []})


def cmd_token_info(args):
    response = request("GET", "/me/token", allow=(404,))
    if response.status == 404:
        emit({"ok": True, "available": False})
        return
    body = response.json if isinstance(response.json, dict) else {}
    body.pop("token", None)
    emit({"ok": True, "available": True, "token": body})


# --- run lifecycle -----------------------------------------------------------


def cmd_run_create(args):
    project_id = need_uuid(args.project, "--project")
    payload = {"agent_label": clean_text(args.agent_label, 120)}
    if args.input_cursor:
        payload["input_cursor"] = validate_cursor(args.input_cursor, "input_cursor")[:2000]
    if args.continuation_from_run_id:
        payload["continuation_from_run_id"] = need_uuid(
            args.continuation_from_run_id, "--continuation-from-run-id")
    key = idempotency_key("runcreate", project_id, payload["agent_label"],
                          time.strftime("%Y-%m-%dT%H", time.gmtime()))
    response = request("POST", "/projects/%s/search-runs" % project_id,
                       payload=payload, idempotency_key=key)
    body = response.json if isinstance(response.json, dict) else {}
    emit({"ok": True, "run_id": body.get("id", ""),
          "prompt_revision": body.get("prompt_revision"),
          "prompt_snapshot_len": len(str(body.get("prompt_snapshot") or ""))})


def _describe_holder(project_id):
    """Read rather than sulk: name the run that currently holds the lease."""
    response = request("GET", "/projects/%s/search-runs?limit=20" % project_id, allow=(404, 422))
    body = response.json if isinstance(response.json, dict) else {}
    for run in body.get("items") or []:
        if isinstance(run, dict) and run.get("status") in ("claimed", "running"):
            return {"run_id": run.get("id", ""), "agent_label": run.get("agent_label", ""),
                    "lease_expires_at": run.get("lease_expires_at", "")}
    return {}


def cmd_run_claim(args):
    project_id = need_uuid(args.project, "--project")
    run_id = need_uuid(args.run, "--run")
    path = "/projects/%s/search-runs/%s/claim" % (project_id, run_id)
    holder = {}
    for index, base in enumerate((0,) + CLAIM_BACKOFF):
        if base:
            delay = base * random.uniform(0.75, 1.25)
            LOG.info("run already claimed; retrying in %.0fs", delay)
            time.sleep(delay)
        response = request("POST", path, payload={}, allow=(409,))
        if response.status != 409:
            body = response.json if isinstance(response.json, dict) else {}
            claim = {
                "project_id": project_id,
                "run_id": run_id,
                "claim_token": body.get("claim_token", ""),
                "lease_expires_at": body.get("lease_expires_at", ""),
                "claimed_at": time.time(),
                "last_heartbeat_at": time.time(),
            }
            REDACTOR.add(claim["claim_token"])
            write_private(claim_path(args), claim)
            emit({"ok": True, "claimed": True, "run_id": run_id,
                  "lease_expires_at": claim["lease_expires_at"]})
            return
        if response.error_code() not in ("run_already_claimed", ""):
            die(EXIT_UNAVAILABLE, "claim refused: %s" % response.error_code())
        if index == 0:
            holder = _describe_holder(project_id)
    # A peer holding a live lease is normal. Deferred is not a failure and is
    # never "skip this project" - the batches this run produced get parked.
    emit({"ok": True, "claimed": False, "deferred": True, "run_id": run_id, "holder": holder})


def cmd_run_heartbeat(args):
    project_id = need_uuid(args.project, "--project")
    run_id = need_uuid(args.run, "--run")
    claim, path = load_claim(args, project_id, run_id)
    elapsed = time.time() - float(claim.get("last_heartbeat_at") or 0)
    if elapsed < HEARTBEAT_MIN_INTERVAL and not args.force:
        emit({"ok": True, "skipped": "not_due", "seconds_since_last": int(elapsed)})
        return
    response = request("POST", "/projects/%s/search-runs/%s/heartbeat" % (project_id, run_id),
                       payload={"claim_token": claim["claim_token"]}, allow=(409,))
    if response.status == 409:
        emit({"ok": False, "lease_lost": True, "code": response.error_code()})
        return
    body = response.json if isinstance(response.json, dict) else {}
    claim["last_heartbeat_at"] = time.time()
    claim["lease_expires_at"] = body.get("lease_expires_at", claim.get("lease_expires_at", ""))
    write_private(path, claim)
    emit({"ok": True, "lease_expires_at": claim["lease_expires_at"]})


def cmd_run_complete(args):
    project_id = need_uuid(args.project, "--project")
    run_id = need_uuid(args.run, "--run")
    claim, path = load_claim(args, project_id, run_id)
    raw = read_json_file(args.payload_file, "--payload-file") if args.payload_file else {}
    if not isinstance(raw, dict):
        die(EXIT_VALIDATION, "--payload-file must hold an object")
    status = args.status or raw.get("status") or "completed"
    if status not in ("completed", "failed"):
        die(EXIT_VALIDATION, "status must be completed or failed")
    payload = {
        "claim_token": claim["claim_token"],
        "status": status,
        "output_cursor": validate_cursor(raw.get("output_cursor"), "output_cursor")[:2000],
        "continuation": validate_continuation(raw.get("continuation")),
        "result_counts": validate_result_counts(raw.get("result_counts") or {}),
        "summary": validate_summary(raw.get("summary")),
    }
    key = idempotency_key("complete", run_id)
    request("POST", "/projects/%s/search-runs/%s/complete" % (project_id, run_id),
            payload=payload, idempotency_key=key)
    try:
        os.remove(path)
    except OSError:
        pass
    emit({"ok": True, "status": status, "result_counts": payload["result_counts"]})


# --- leads -------------------------------------------------------------------


def _load_items(path):
    """Accept {"items": [...]}, a bare list, or JSON Lines."""
    if path == "-":
        text = sys.stdin.read()
    else:
        try:
            with open(path, "rb") as handle:
                text = handle.read().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            die(EXIT_USAGE, "cannot read --items-file %s: %s" % (path, exc))
    text = text.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed.append(json.loads(line))
            except ValueError:
                die(EXIT_USAGE, "--items-file is neither JSON nor JSON Lines")
    if isinstance(parsed, dict):
        parsed = parsed.get("items") or []
    if not isinstance(parsed, list):
        die(EXIT_USAGE, "--items-file must hold a list of leads")
    return parsed


def _park(park_dir, project_id, lane, key, payload, counters):
    directory = os.path.join(park_dir, project_id)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    cutoff = time.time() - PARK_MAX_AGE_DAYS * 86400
    existing = []
    for name in sorted(os.listdir(directory)):
        full = os.path.join(directory, name)
        try:
            if os.path.getmtime(full) < cutoff:
                os.remove(full)
            else:
                existing.append(full)
        except OSError:
            pass
    if len(existing) >= PARK_MAX_FILES:
        counters["parked_dropped"] += 1
        LOG.error("parked-batch directory is full (%d); dropping this batch", PARK_MAX_FILES)
        return ""
    name = "%s-%s.json" % (re.sub(r"[^a-z0-9:-]", "", lane) or "lane", digest(key)[:16])
    path = os.path.join(directory, name.replace(":", "_"))
    write_private(path, {"protocol": 1, "project_id": project_id, "lane": lane,
                         "created_at": now_iso(), "created_ts": time.time(),
                         "idempotency_key": key, "payload": payload})
    counters["parked"] += 1
    return path


def _post_batch(project_id, payload, key, counters):
    response = request("POST", "/projects/%s/leads/bulk-upsert" % project_id,
                       payload=payload, idempotency_key=key, allow=(409,))
    if response.status == 409:
        code = response.error_code()
        if code == "idempotency_key_reused":
            die(EXIT_VALIDATION, "idempotency key reused with a different payload")
        counters["conflicts"] += len(payload["items"])
        LOG.info("batch conflict (%s); leaving the stored values alone", code or "409")
        return []
    body = response.json if isinstance(response.json, dict) else {}
    results = body.get("results") or []
    written = []
    for result in results:
        if not isinstance(result, dict):
            continue
        outcome = result.get("outcome")
        if outcome in ("created", "updated", "unchanged"):
            counters[outcome] += 1
            lead = result.get("lead") or {}
            if outcome != "unchanged" and lead.get("id"):
                written.append(lead)
        elif outcome == "conflict":
            counters["conflicts"] += 1
        elif outcome == "error":
            code = ""
            err = result.get("error") or {}
            if isinstance(err, dict):
                code = str(err.get("code") or "")
            if code == "lead_trashed":
                # Never work around this. No identity mutation, no restore.
                counters["unchanged_trashed"] += 1
            elif code == "stale_write":
                counters["conflicts"] += 1  # a human is editing; keep their value
            else:
                counters["errors"] += 1
    return written


def _verify_sample(project_id, written, sample, counters):
    """Bounded write-then-verify: re-read a few leads and confirm what stuck."""
    for lead in written[:sample]:
        lead_id = str(lead.get("id") or "")
        if not _UUID.match(lead_id):
            continue
        response = request("GET", "/projects/%s/leads/%s" % (project_id, lead_id), allow=(404,))
        if response.status == 404:
            counters["verify_failed"] += 1
            continue
        stored = response.json if isinstance(response.json, dict) else {}
        if stored.get("url") != lead.get("url") or stored.get("title") != lead.get("title"):
            counters["verify_failed"] += 1


def cmd_leads_upsert(args):
    project_id = need_uuid(args.project, "--project")
    counters = dict((k, 0) for k in (
        "created", "updated", "unchanged", "unchanged_trashed", "conflicts", "errors",
        "validation_dropped", "validation_truncated", "parked", "parked_dropped",
        "drained", "verify_failed", "batches"))
    problems = []

    if args.drain_parked and args.park_dir:
        _drain(project_id, args.park_dir, counters)

    items = _load_items(args.items_file) if args.items_file else []
    clean_items = []
    for item in items:
        clean, notes = validate_lead(item)
        if clean is None:
            counters["validation_dropped"] += 1
            problems.extend(notes[:2])
            continue
        if notes:
            counters["validation_truncated"] += 1
            problems.extend(notes[:2])
        if args.run_id and "search_run_id" not in clean:
            clean["search_run_id"] = need_uuid(args.run_id, "--run-id")
        clean_items.append(clean)

    if args.max_leads and len(clean_items) > args.max_leads:
        LOG.info("capping %d leads to %d", len(clean_items), args.max_leads)
        clean_items = clean_items[:args.max_leads]

    for start in range(0, len(clean_items), MAX_BATCH_ITEMS):
        batch = clean_items[start:start + MAX_BATCH_ITEMS]
        payload = {"items": batch}
        key = idempotency_key("upsert", project_id,
                              json.dumps(payload, sort_keys=True, separators=(",", ":")))
        if args.defer:
            if not args.park_dir:
                die(EXIT_USAGE, "--defer needs --park-dir")
            _park(args.park_dir, project_id, args.lane or "unknown:lane", key, payload, counters)
            continue
        counters["batches"] += 1
        written = _post_batch(project_id, payload, key, counters)
        if args.verify_sample:
            _verify_sample(project_id, written, args.verify_sample, counters)

    ok = counters["errors"] == 0 and counters["verify_failed"] == 0
    emit({"ok": ok, "counts": counters, "problems": sorted(set(problems))[:10]})
    if not ok:
        sys.exit(EXIT_UNAVAILABLE)


def _drain(project_id, park_dir, counters):
    directory = os.path.join(park_dir, project_id)
    if not os.path.isdir(directory):
        return
    cutoff = time.time() - PARK_MAX_AGE_DAYS * 86400
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        try:
            with open(path) as handle:
                parked = json.load(handle)
        except (OSError, ValueError):
            LOG.info("discarding unreadable parked batch %s", name)
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        created = float(parked.get("created_ts") or 0)
        if created < cutoff:
            os.remove(path)
            continue
        payload = parked.get("payload") or {}
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            os.remove(path)
            continue
        key = str(parked.get("idempotency_key") or "")
        if time.time() - created > PARK_REKEY_AFTER_DAYS * 86400 or not key:
            # Server-side keys expire at 7 days: same bytes, fresh key.
            key = idempotency_key("upsert", project_id, digest(path, now_iso()))
        counters["batches"] += 1
        # observed_at is never bumped: a delayed write stays honest about when
        # the listing was actually seen.
        _post_batch(project_id, payload, key, counters)
        counters["drained"] += 1
        try:
            os.remove(path)
        except OSError:
            pass


def cmd_comment_add(args):
    project_id = need_uuid(args.project, "--project")
    lead_id = need_uuid(args.lead, "--lead")
    if args.body_file == "-":
        raw = sys.stdin.read()
    else:
        try:
            with open(args.body_file, "rb") as handle:
                raw = handle.read().decode("utf-8", "replace")
        except OSError as exc:
            die(EXIT_USAGE, "cannot read --body-file: %s" % exc)
    body = clean_text(raw.strip(), 10000)
    if not body:
        die(EXIT_USAGE, "refuse: empty comment")
    key = idempotency_key("comment", project_id, lead_id, digest(body))
    response = request("POST", "/projects/%s/leads/%s/comments" % (project_id, lead_id),
                       payload={"body": body}, idempotency_key=key)
    created = response.json if isinstance(response.json, dict) else {}
    emit({"ok": True, "comment_id": created.get("id", ""), "chars": len(body)})


# --- CLI ---------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="homing.py",
        description="Homing API client. The only code that touches the account key.",
        epilog=("The key never appears in arguments, environment values, output, or logs. "
                "There are no destructive subcommands and there never will be: removal is "
                "suggested in a comment, never performed. Exit codes are documented at the "
                "top of this file; run with --verbose for redacted progress on stderr."),
    )
    parser.add_argument("--verbose", action="store_true", help="redacted debug logging on stderr")
    subparsers = parser.add_subparsers(dest="command")

    def add(name, help_text, needs_project=True):
        sub = subparsers.add_parser(name, help=help_text, description=help_text)
        if needs_project:
            sub.add_argument("--project", required=True, metavar="UUID")
        return sub

    subparsers.add_parser("projects", help="list every project this key can see")
    add("project", "read one project, its current prompt, and its ETag")
    add("prompt", "read one project's current prompt")

    changes = add("changes", "read the change feed from a cursor")
    changes.add_argument("--cursor", default="")
    changes.add_argument("--cursor-file", default="", help="read and update a stored cursor")
    changes.add_argument("--limit", type=int, default=100)

    create = add("run-create", "create a search run (snapshots the prompt)")
    create.add_argument("--agent-label", required=True)
    create.add_argument("--input-cursor", default="")
    create.add_argument("--continuation-from-run-id", default="")

    claim = add("run-claim", "claim the run lease; parks and defers if a peer holds it")
    claim.add_argument("--run", required=True, metavar="UUID")
    claim.add_argument("--claim-file", default="")

    beat = add("run-heartbeat", "renew the lease (only once the write phase exceeds ~4 min)")
    beat.add_argument("--run", required=True, metavar="UUID")
    beat.add_argument("--claim-file", default="")
    beat.add_argument("--force", action="store_true", help="ignore the minimum interval")

    complete = add("run-complete", "complete or fail the run with a closed-schema payload")
    complete.add_argument("--run", required=True, metavar="UUID")
    complete.add_argument("--claim-file", default="")
    complete.add_argument("--payload-file", default="", metavar="PATH")
    complete.add_argument("--status", choices=("completed", "failed"), default="")

    upsert = add("leads-upsert", "bulk-upsert leads in batches of at most 100")
    upsert.add_argument("--items-file", default="", metavar="PATH",
                        help="JSON {items:[...]}, a bare list, or JSON Lines; - for stdin")
    upsert.add_argument("--run-id", default="", help="stamp this search_run_id on each lead")
    upsert.add_argument("--park-dir", default="", metavar="DIR")
    upsert.add_argument("--lane", default="", help="lane slug recorded on a parked batch")
    upsert.add_argument("--defer", action="store_true",
                        help="park the batch instead of writing (use when the claim deferred)")
    upsert.add_argument("--drain-parked", action="store_true",
                        help="replay parked batches for this project first")
    upsert.add_argument("--max-leads", type=int, default=0)
    upsert.add_argument("--verify-sample", type=int, default=5,
                        help="re-read this many written leads to confirm what stuck")

    comment = add("comment-add", "append a plain-text comment to a lead")
    comment.add_argument("--lead", required=True, metavar="UUID")
    comment.add_argument("--body-file", required=True, metavar="PATH")

    subparsers.add_parser("token-info", help="key scopes and expiry, without a second credential")
    return parser


COMMANDS = {
    "projects": cmd_projects,
    "project": cmd_project,
    "prompt": cmd_prompt,
    "changes": cmd_changes,
    "run-create": cmd_run_create,
    "run-claim": cmd_run_claim,
    "run-heartbeat": cmd_run_heartbeat,
    "run-complete": cmd_run_complete,
    "leads-upsert": cmd_leads_upsert,
    "comment-add": cmd_comment_add,
    "token-info": cmd_token_info,
}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(getattr(args, "verbose", False))
    if not args.command:
        parser.print_help()
        return EXIT_USAGE
    try:
        COMMANDS[args.command](args)
    except SystemExit:
        raise
    except Exception as exc:  # never let a traceback carry a fragment of the key
        die(EXIT_UNAVAILABLE, "%s: %s" % (type(exc).__name__, exc))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
