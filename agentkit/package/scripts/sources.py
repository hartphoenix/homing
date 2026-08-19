#!/usr/bin/env python3
"""sources.py - fetch listing sources and extract bounded records.

This script has NO account key, NO Homing access, and NO model. It is the half
of the runtime that touches untrusted content, which is exactly why it holds no
credential: a fully hijacked parse still has nothing to exfiltrate with. Its
counterpart, homing.py, holds the credential and never sees page text.

Run it. Do not read it into a model's context.

    sources.py --help
    sources.py probe    --url https://example.test/rent --slug example --install-probe
    sources.py calibrate --slug example --url-populated URL --url-empty URL
    sources.py fetch    --slug example --sources sources.json --out-dir run/raw
    sources.py extract  --slug example --sources sources.json --in-dir run/raw \\
                        --out run/candidates.jsonl

Rules this file enforces mechanically:

  * Requests are shaped like the browser on this machine, at a human pace, from
    the user's own connection: a few pages a day, reading listings published
    publicly for renters to read. Crawl-delay and rate limits are honoured and
    responses are cached, so a page is fetched once rather than repeatedly.
    There is no CAPTCHA-solving service, no rotating proxy pool, and no replay
    of another session's bot-management cookie anywhere in this file.
  * It fetches only hosts listed verbatim in sources.json, matched as whole
    strings. A URL found inside fetched content is fetchable only if its host is
    on that list AND its path matches that source's listing pattern. Nothing
    discovered in content can widen the list.
  * robots.txt is fetched first and obeyed, evaluated against the one group that
    matches the identity actually sent (RFC 9309). A 4xx is "unavailable", which
    the standard defines as no restrictions, so the fetch proceeds at the normal
    polite rate. A 5xx, a network error, or a 200 that is not text/plain is a
    non-answer: the source goes into a temporary cooldown and is re-probed. Only
    a retrievable robots.txt that disallows the path is the publisher's own
    durable answer, and only that - or three consecutive blocks - retires a
    source. A transient blip never retires anything.
  * The sitemap channel is discovery-only. It yields listing URLs and lastmod
    dates; no detail page is fetched, so title, price and location come back
    empty by design rather than by failure.
  * EMPTY-GENUINE is the only zero it will report as "nothing new". Every other
    zero reports SOURCE-UNCHECKED. Reporting a block as an empty result lies to
    someone looking for a home.
  * Emitted records are bounded, typed, truncated, stripped of control
    characters and angle brackets, and capped in count. Raw page HTML never
    leaves this script.

Exit codes:
    0   success - including a "blocked" classification, which is data, not a crash
   64   usage error, or a URL this script refuses to fetch
   69   network failure after the one permitted retry
   70   a hard bound was violated
   73   sources.json or the state file failed its closed-schema check
   77   robots.txt disallows the path for the identity sent (consent, not a retry)
   78   not installed, or sources.json is missing
"""

import argparse
import gzip
import http.client
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET

ORIGIN = "__HOMING_ORIGIN__"  # replaced with the real origin by the installer

# Listing pages serve their JSON-LD to a normal browser and withhold it from
# anything self-identifying as a crawler. At this scale - a few pages a day, from
# the user's own machine, for the people who will live in the home - a crawler
# string costs the user the data without sparing the site any load. Requests are
# shaped like the browser on this machine. Rate limits and Crawl-delay are still
# honoured, and there is no CAPTCHA-solving or proxy-rotation path anywhere here.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/140.0.0.0 Safari/537.36")
CRAWLER_UA = ("HomingAgent/1.0 (+%s/about/agent; user-directed housing search "
              "on behalf of one person)" % ORIGIN)
USER_AGENT = BROWSER_UA          # overridable via --ua for A/B reachability tests
UA_TOKEN = "*"                   # which robots.txt group applies to us
ACCEPT_HTML = ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8")
ACCEPT_LANGUAGE = "en-US,en;q=0.9"


def set_user_agent(mode):
    """Switch the presented UA. 'browser' (default) or 'crawler'."""
    global USER_AGENT, UA_TOKEN
    if mode == "crawler":
        USER_AGENT, UA_TOKEN = CRAWLER_UA, "HomingAgent"
    else:
        USER_AGENT, UA_TOKEN = BROWSER_UA, "*"

MAX_FETCH_BYTES = 2 * 1024 * 1024   # hard read ceiling for any response
MAX_PAGE_BYTES = 200 * 1024         # an HTML page above this is skipped: oversize
MAX_PAGES = 3                       # per source, per run
MAX_RECORDS = 40                    # per project
MAX_RECORD_BYTES = 600              # per record, serialized
MAX_SEEN_IDS = 200
MAX_LD_NODES = 5000                 # bounded JSON-LD walk: nodes visited
MAX_LD_DEPTH = 12                   # bounded JSON-LD walk: nesting depth
MAX_REVALIDATIONS = 40
DEFAULT_INTERVAL = 2.0              # seconds between requests to one host
TIMEOUT = 30
ROBOTS_TTL = 86400
OVERLAP_SECONDS = 900               # re-read 15 min behind the cursor; dedup absorbs it

EXIT_OK = 0
EXIT_USAGE = 64
EXIT_NETWORK = 69
EXIT_BOUND = 70
EXIT_SCHEMA = 73
EXIT_CONSENT = 77
EXIT_CONFIG = 78

RETIREMENT_DAYS = (7, 30)  # 1st block -> 7d, 2nd -> 30d, 3rd -> retired
TRANSIENT_COOLDOWN_SECONDS = 3600        # network error, rate limit: try again next hour
ROBOTS_COOLDOWN_SECONDS = 6 * 3600       # robots.txt did not answer: back off, do not retire

CHANNELS = ("rss", "sitemap", "json", "html")


def log(message):
    sys.stderr.write("sources: %s\n" % message)


def die(code, message):
    log(message)
    sys.exit(code)


def emit(obj):
    sys.stdout.write(json.dumps(obj, sort_keys=True) + "\n")
    sys.stdout.flush()


def now():
    return time.time()


def iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else now()))


# --- the block classifier ----------------------------------------------------
#
# Two layers, because runtimes differ: header rules when headers exist, body
# markers always. Status is not the signal - measured blocks arrived as 403,
# 401 with no login form, 429 with no Retry-After, and 202 with an empty body.

def _header_vendor(headers):
    head = dict((k.lower(), (v or "").lower()) for k, v in headers.items())
    cookies = head.get("set-cookie", "")
    if "cf-mitigated" in head:
        return "cloudflare"
    if "cloudflare" in head.get("server", "") and "cf-ray" in head:
        return "cloudflare"
    if "__cf_bm=" in cookies or "chlray;desc=" in head.get("server-timing", ""):
        return "cloudflare"
    if "x-amzn-waf-action" in head:          # regardless of status, incl. 202
        return "aws-waf"
    if "x-px-blocked" in head or "_pxhd=" in cookies or "_px3=" in cookies:
        return "perimeterx"
    if "x-datadome" in head or "datadome" in head.get("server", "") or "datadome=" in cookies:
        return "datadome"
    if "akamaighost" in head.get("server", "") or "akamai-grn" in head:
        return "akamai"
    if "_abck=" in cookies or "bm_sz=" in cookies or "aka_a2=" in cookies:
        return "akamai"
    if "kp_uidz" in cookies:
        return "kasada"
    if "x-iinfo" in head or "incap_ses_" in cookies or "visid_incap_" in cookies:
        return "imperva"
    if "error from cloudfront" in head.get("x-cache", ""):
        return "cloudfront"
    return ""


BODY_MARKERS = (
    ("just a moment", "cloudflare"),
    ("/cdn-cgi/challenge-platform", "cloudflare"),
    ("enable javascript and cookies to continue", "cloudflare"),
    ("px-captcha", "perimeterx"),
    ("_pxappid", "perimeterx"),
    ("_pxonerror", "perimeterx"),
    ("geo.captcha-delivery.com", "datadome"),
    ("datadome", "datadome"),
    ("errors.edgesuite.net", "akamai"),
    ("window.kpsdk", "kasada"),
    ("_incapsula_resource", "imperva"),
    ("incapsula incident id", "imperva"),
    ("pardon our interruption", "imperva"),
    ("request unsuccessful", "imperva"),
    ("verify you are human", "generic"),
    ("are you a robot", "generic"),
    ("unusual traffic", "generic"),
)

GEO_MARKERS = ("not available in your country", "not available in your region",
               "content is not available in your location")
DEAD_MARKERS = re.compile(
    r"no longer available|listing has been removed|has been let|let agreed|under offer"
    r"|off market|rented|vermietet|reserviert|vendu|alquilado|verhuurd", re.I)
SPA_MARKERS = ('<div id="root"></div>', '<div id="__next"></div>',
               '<script type="module"', "/assets/index-")
PASSWORD_FORM = re.compile(r'type=["\']password["\']|name=["\']password["\']', re.I)
PRICE_HINT = re.compile(r"[$\u20ac\u00a3\u00a5]\s?\d|\d\s?(?:usd|eur|gbp|per month|/mo|pcm)", re.I)


def _body_vendor(text):
    lowered = text[:200000].lower()
    for marker, vendor in BODY_MARKERS:
        if marker in lowered:
            return vendor
    return ""


def _path_family(url):
    parts = urllib.parse.urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    return (parts.netloc.lower(), segments[0].lower() if segments else "")


def classify(result, fingerprint, prior):
    """Return (classification, vendor). Order is fixed; see BRIEF 5.4."""
    fingerprint = fingerprint or {}
    text = result.get("text") or ""
    lowered = text[:200000].lower()

    vendor = _header_vendor(result.get("headers") or {}) or _body_vendor(text)
    if vendor:
        return "BLOCKED-EDGE", vendor

    status = result.get("status") or 0
    head = dict((k.lower(), (v or "")) for k, v in (result.get("headers") or {}).items())

    if status == 304:
        return "NOT-MODIFIED", ""
    if status == 429:
        rated = "retry-after" in head or any(k.startswith("ratelimit") or
                                             k.startswith("x-ratelimit") for k in head)
        # A 429 with no Retry-After and no quota header is a wall wearing a
        # rate-limit costume (measured on two portals). Assume the worst.
        return ("RATE-LIMITED", "") if rated else ("BLOCKED-EDGE", "unbranded")
    if status == 451 or any(m in lowered for m in GEO_MARKERS):
        return "GEOFENCED", ""
    if status in (401, 403, 503):
        if PASSWORD_FORM.search(text):
            return "LOGIN-WALL", ""
        # robots says yes, the network says no: an address-reputation block.
        if prior.get("robots_allows") and len(text) < 2048:
            return "BLOCKED-IP", ""
        return "BLOCKED-UNKNOWN", ""
    if not (200 <= status < 300):
        return "BLOCKED-UNKNOWN", ""
    if PASSWORD_FORM.search(text) and "sign in" in lowered:
        return "LOGIN-WALL", ""

    if _path_family(result["final_url"]) != _path_family(result["url"]):
        return "SILENT-DEGRADATION", ""   # soft-404: 200, big, chromed, no listings

    min_ok = int(fingerprint.get("min_ok_bytes") or 0)
    if min_ok and result["bytes"] < min_ok:
        return "BLOCKED-UNKNOWN", ""
    markers = fingerprint.get("shell_markers") or []
    if markers and not all(m.lower() in lowered for m in markers):
        return "BLOCKED-UNKNOWN", ""

    if result.get("content_type", "").startswith("text/html") and 1024 <= result["bytes"] <= 61440:
        if ("application/ld+json" not in lowered and 'property="og:title"' not in lowered
                and not PRICE_HINT.search(text)
                and any(m.lower() in lowered for m in SPA_MARKERS)):
            return "BLOCKED-JS", ""

    if prior.get("body_hash") and prior["body_hash"] == result["body_hash"] \
            and prior.get("last_url") and prior["last_url"] != result["url"]:
        return "POISONED", ""

    # HTML is read only to MAX_PAGE_BYTES. A complete JSON-LD block in that
    # bounded prefix is useful; a truncated prefix with no complete records is
    # not evidence that the source is genuinely empty.
    if result.get("truncated") and result.get("listings", 0) == 0:
        return "SKIPPED-OVERSIZE", ""
    if result.get("listings", 0) == 0:
        return "EMPTY-GENUINE", ""
    return "OK", ""


REPORTABLE_ZERO = "EMPTY-GENUINE"
BLOCK_FAMILY = ("BLOCKED-EDGE", "BLOCKED-IP", "BLOCKED-JS", "BLOCKED-UNKNOWN",
                "LOGIN-WALL", "GEOFENCED", "SILENT-DEGRADATION", "POISONED")
# Nothing here is a source decision. These are conditions of the moment - the
# network, the edge's mood, a robots.txt that did not answer - so they buy a
# cooldown and never advance the retirement ladder.
TRANSIENT_FAMILY = ("NETWORK-ERROR", "RATE-LIMITED", "ROBOTS-UNAVAILABLE",
                    "SKIPPED-COOLDOWN", "SKIPPED-OVERSIZE", "SOURCE-UNCHECKED")
HEALTHY_ZERO = ("EMPTY-GENUINE", "NOT-MODIFIED")


def report_as(classification, count):
    if classification == "OK" and count:
        return "ok"
    if classification == REPORTABLE_ZERO:
        return "nothing_new"
    if classification == "NOT-MODIFIED":
        return "nothing_new"
    return "source_unchecked"


# --- allowlist and config ----------------------------------------------------

_HOST = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_LANE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}:[a-z0-9][a-z0-9-]{0,39}$")
_ID_OK = re.compile(r"^[A-Za-z0-9_.:~-]{1,64}$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_WS = re.compile(r"\s+")


ID_RULE_FORMS = """path_segment:<index> | path:<index> | query:<name> | feed:guid | guid
    | jsonld:@id | jsonld:identifier | jsonld:sku | jsonld:url | kyero:<field>
    | reddit:fullname"""


def valid_id_rule(rule):
    """The id_rule forms references/sources.md documents, and nothing else.

    An unrecognised rule used to fall through to the generic branch and quietly
    produce a different id than the one the source record claimed, which forks
    one listing into two leads. It is a configuration error, so say so.
    """
    rule = str(rule or "").strip()
    if not rule:
        return True                      # absent is legal: use whatever the parser found
    name, sep, arg = rule.partition(":")
    name = name.lower()
    if name in ("path", "path_segment"):
        if not sep:
            return False
        try:
            int(arg)
        except ValueError:
            return False
        return True
    if name == "query":
        return bool(arg)
    if name == "guid":
        return arg in ("", "guid", "id")
    if name == "feed":
        return arg in ("guid", "id")
    if name == "jsonld":
        return arg in ("@id", "identifier", "sku", "url")
    if name == "kyero":
        return bool(arg)
    if name == "reddit":
        return arg in ("fullname", "name", "id")
    return False


def load_sources(path):
    if not path:
        die(EXIT_CONFIG, "--sources is required; it is the fetch allowlist")
    try:
        with open(path, "rb") as handle:
            config = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        die(EXIT_CONFIG, "cannot read sources.json (%s): %s" % (path, exc))
    if not isinstance(config, dict):
        die(EXIT_SCHEMA, "sources.json must be an object")
    hosts = config.get("allowed_hosts") or []
    if not isinstance(hosts, list) or not hosts:
        die(EXIT_SCHEMA, "sources.json needs a non-empty allowed_hosts list")
    for host in hosts:
        if not isinstance(host, str) or not _HOST.match(host.lower()):
            die(EXIT_SCHEMA, "allowed_hosts holds something that is not a hostname")
    config["allowed_hosts"] = [h.lower() for h in hosts]
    sources = config.get("sources") or []
    if not isinstance(sources, list):
        die(EXIT_SCHEMA, "sources.json sources must be a list")
    for source in sources:
        if not isinstance(source, dict) or not _SLUG.match(str(source.get("slug", ""))):
            die(EXIT_SCHEMA, "every source needs a slug")
        if source.get("channel") not in CHANNELS:
            die(EXIT_SCHEMA, "source %s has an unknown channel" % source.get("slug"))
        if source.get("lane") and not _LANE.match(str(source["lane"])):
            die(EXIT_SCHEMA, "source %s has a malformed lane" % source["slug"])
        if not valid_id_rule(source.get("id_rule")):
            die(EXIT_SCHEMA, "source %s has an unknown id_rule %r; the documented forms are: %s"
                % (source["slug"], source.get("id_rule"), " ".join(ID_RULE_FORMS.split())))
        pattern = source.get("listing_url_pattern")
        if pattern is not None:
            if not isinstance(pattern, str) or not pattern.strip():
                die(EXIT_SCHEMA,
                    "source %s has an empty listing_url_pattern" % source["slug"])
            try:
                re.compile(pattern)
            except re.error as exc:
                die(EXIT_SCHEMA, "source %s has an invalid listing_url_pattern: %s"
                    % (source["slug"], exc))
    return config


def find_source(config, slug):
    for source in config.get("sources") or []:
        if source.get("slug") == slug:
            return source
    die(EXIT_USAGE, "no source named %r in sources.json" % slug)


def host_allowed(config, url):
    """Whole-string host match. Suffix matching is where allowlists die."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        return False, "only https URLs are fetched"
    host = (parts.hostname or "").lower()
    if host not in config["allowed_hosts"]:
        return False, "host %s is not in sources.json" % host
    return True, ""


def content_url_allowed(config, source, url):
    """A URL discovered inside fetched content needs BOTH gates."""
    ok, reason = host_allowed(config, url)
    if not ok:
        return False, reason
    pattern = source.get("listing_url_pattern")
    if not pattern:
        return False, "source has no listing_url_pattern"
    try:
        if not re.search(pattern, url):
            return False, "url does not match this source's listing pattern"
    except re.error:
        return False, "listing_url_pattern is not a valid regex"
    return True, ""


# --- state (closed schema; anything that fails validation resets) ------------

STATE_STR_FIELDS = ("status", "vendor", "last_checked", "next_eligible", "egress_class",
                    "etag", "last_modified", "cursor", "body_hash", "last_url")


def blank_source_state():
    return {"status": "unknown", "vendor": "", "consecutive_blocks": 0,
            "last_checked": "", "next_eligible": "", "egress_class": "unknown",
            "etag": "", "last_modified": "", "cursor": "", "body_hash": "",
            "last_url": "", "seen_ids": [], "robots_allows": False,
            "robots_checked_at": 0, "crawl_delay": 0, "last_fetch_at": 0}


def load_state(path):
    state = {"protocol": 1, "sources": {}, "hosts": {}, "state_reset": 0}
    if not path or not os.path.exists(path):
        return state
    try:
        with open(path, "rb") as handle:
            raw = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        log("state file unreadable; starting from a fresh snapshot")
        state["state_reset"] += 1
        return state
    if not isinstance(raw, dict):
        state["state_reset"] += 1
        return state
    for slug, entry in (raw.get("sources") or {}).items():
        clean = blank_source_state()
        if not _SLUG.match(str(slug)) or not isinstance(entry, dict):
            state["state_reset"] += 1
            continue
        bad = False
        for key in STATE_STR_FIELDS:
            value = entry.get(key, "")
            if not isinstance(value, str) or len(value) > 256 or _CONTROL.search(value):
                bad = True
                break
            clean[key] = value
        ids = entry.get("seen_ids") or []
        if not isinstance(ids, list) or not all(
                isinstance(i, str) and _ID_OK.match(i) for i in ids):
            bad = True
        if bad:
            state["state_reset"] += 1
            state["sources"][slug] = blank_source_state()
            continue
        clean["seen_ids"] = ids[-MAX_SEEN_IDS:]
        for key in ("consecutive_blocks", "robots_checked_at", "crawl_delay", "last_fetch_at"):
            try:
                clean[key] = max(0, float(entry.get(key) or 0))
            except (TypeError, ValueError):
                clean[key] = 0
        clean["consecutive_blocks"] = int(clean["consecutive_blocks"])
        clean["robots_allows"] = bool(entry.get("robots_allows"))
        state["sources"][slug] = clean
    hosts = raw.get("hosts") or {}
    if isinstance(hosts, dict):
        for host, when in hosts.items():
            if isinstance(host, str) and _HOST.match(host.lower()):
                try:
                    state["hosts"][host.lower()] = float(when)
                except (TypeError, ValueError):
                    pass
    return state


def save_state(path, state):
    if not path:
        return
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(state, sort_keys=True))


def source_state(state, slug):
    return state["sources"].setdefault(slug, blank_source_state())


def apply_retirement(entry, classification):
    """1st block -> 7 days, 2nd -> 30 days, 3rd -> retired. Only a user re-enables.

    A transient failure is neither a block nor a success: it takes a short
    cooldown, leaves the block count where it was, and can never retire the
    source. Retirement is reserved for a durable answer from the source itself.
    """
    if classification in BLOCK_FAMILY:
        entry["consecutive_blocks"] = int(entry.get("consecutive_blocks", 0)) + 1
        blocks = entry["consecutive_blocks"]
        if blocks >= 3:
            entry["status"] = "retired"
            entry["next_eligible"] = ""
        else:
            entry["status"] = "blocked"
            entry["next_eligible"] = iso(now() + RETIREMENT_DAYS[blocks - 1] * 86400)
    elif classification in TRANSIENT_FAMILY:
        entry["status"] = "cooldown"
        entry["next_eligible"] = iso(now() + TRANSIENT_COOLDOWN_SECONDS)
    else:
        entry["consecutive_blocks"] = 0
        entry["status"] = "ok"
        entry["next_eligible"] = ""


def eligible(entry, egress_class):
    if entry.get("egress_class", "unknown") != egress_class:
        # The runtime moved. A source unreachable from a datacenter may be fine
        # from the user's own connection, so the block table does not carry over.
        return True, "egress class changed; re-probing"
    if entry.get("status") == "retired":
        return False, "retired after three blocks; only you can re-enable it"
    nxt = entry.get("next_eligible") or ""
    if nxt and _ISO.match(nxt):
        due = time.strptime(nxt[:19], "%Y-%m-%dT%H:%M:%S")
        if time.time() < time.mktime(due) - time.timezone:
            return False, "in cooldown until %s" % nxt
    return True, ""


# --- HTTP --------------------------------------------------------------------


class _HostCheckRedirect(urllib.request.HTTPRedirectHandler):
    """Re-check the host on every hop. A redirect is not a way onto a new host."""

    def __init__(self, allowed):
        self.allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme != "https" or (parts.hostname or "").lower() not in self.allowed:
            raise urllib.error.URLError("redirect left the allowlist: %s" % parts.netloc)
        return urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl)


def http_get(url, allowed_hosts, etag="", last_modified="", method="GET", max_bytes=None):
    """One request, shaped like the browser on this machine would send it."""
    opener = urllib.request.build_opener(_HostCheckRedirect(set(allowed_hosts)))
    opener.addheaders = []
    headers = {"User-Agent": USER_AGENT,
               "Accept": ACCEPT_HTML,
               "Accept-Language": ACCEPT_LANGUAGE}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    request = urllib.request.Request(url, headers=headers, method=method)
    cap = max_bytes or MAX_FETCH_BYTES
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            final = response.geturl()
            status = response.status
            head = dict(response.headers.items())
            try:
                body = response.read(cap + 1)
            except http.client.IncompleteRead as exc:
                # Large listing pages are chunked and routinely end short. What
                # arrived is still a real page; discarding a megabyte of valid
                # HTML over a missing terminator would report a working source
                # as a network error.
                body = exc.partial
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(cap + 1) if exc.fp else b""
        except http.client.IncompleteRead as inner:
            body = inner.partial
        final, status, head = exc.geturl(), exc.code, dict(exc.headers.items())
    except (urllib.error.URLError, OSError) as exc:
        return {"url": url, "final_url": url, "status": 0, "headers": {}, "bytes": 0,
                "text": "", "body": b"", "content_type": "", "body_hash": "",
                "error": str(getattr(exc, "reason", exc))}

    truncated = len(body) > cap
    body = body[:cap]
    if head.get("Content-Encoding", "").lower() == "gzip" or url.endswith(".gz"):
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError):
            pass
    content_type = (head.get("Content-Type") or "").split(";")[0].strip().lower()
    return {"url": url, "final_url": final, "status": status, "headers": head,
            "bytes": len(body), "text": body.decode("utf-8", "replace"), "body": body,
            "content_type": content_type, "truncated": truncated,
            "body_hash": hashlib.sha256(body).hexdigest()[:32], "error": ""}


def wait_for_host(state, host, interval):
    last = float(state["hosts"].get(host, 0))
    gap = interval - (now() - last)
    if gap > 0:
        time.sleep(min(gap, 30))
    state["hosts"][host] = now()


# --- robots ------------------------------------------------------------------


def check_robots(url, allowed_hosts):
    """Returns (allowed, crawl_delay, sitemaps, status).

    Follows RFC 9309 s2.3.1 on unreachable robots.txt: 4xx means "unavailable",
    which the standard defines as no restrictions, so we proceed at our normal
    polite rate. 5xx means "unreachable" and is treated as a temporary full
    disallow. A CDN answering 403 to a robots.txt request is that CDN filtering
    the fetch, not the publisher expressing a policy - treating it as a refusal
    retires sources that never asked to be left alone.
    """
    parts = urllib.parse.urlsplit(url)
    robots_url = "%s://%s/robots.txt" % (parts.scheme, parts.netloc)
    result = http_get(robots_url, allowed_hosts, max_bytes=512 * 1024)
    status = result["status"]
    if status >= 500 or (status == 0 and result["error"]):
        return False, 0, [], status or -1
    if status != 200:
        return True, 0, [], 200          # RFC 9309: unavailable => allow all
    if result["content_type"] and result["content_type"] not in ("text/plain", "text/x-robots"):
        # 200 text/html is a challenge or an SPA, not robots (measured on funda.nl).
        return False, 0, [], -1
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(result["text"].splitlines())
    delay = parser.crawl_delay(UA_TOKEN) or 0
    try:
        sitemaps = parser.site_maps() or []
    except Exception:
        sitemaps = []
    return parser.can_fetch(UA_TOKEN, url), float(delay), sitemaps, 200


# --- parsers -----------------------------------------------------------------


def _clean(value, limit):
    text = _CONTROL.sub(" ", str(value or ""))
    text = text.replace("<", "").replace(">", "")   # no forged delimiters, no markup
    return _WS.sub(" ", text).strip()[:limit]


def _strip_tags(html):
    return re.sub(r"<[^>]{0,400}>", " ", html or "")


def _localname(tag):
    return tag.split("}")[-1].lower()


def parse_rss(text):
    """RSS 2.0 and Atom via ElementTree. Returns raw dicts, not records."""
    try:
        root = ET.fromstring(text.encode("utf-8", "replace"))
    except ET.ParseError as exc:
        return [], "xml parse error: %s" % exc
    items = []
    for node in root.iter():
        if _localname(node.tag) not in ("item", "entry"):
            continue
        row = {}
        for child in node:
            name = _localname(child.tag)
            if name == "link":
                row["url"] = child.get("href") or (child.text or "").strip() or row.get("url", "")
            elif name in ("title", "summary", "description", "content"):
                row[name] = _strip_tags(child.text or "")
            elif name in ("guid", "id"):
                row["native_id"] = (child.text or "").strip()
            elif name in ("pubdate", "published", "updated"):
                row["posted"] = (child.text or "").strip()
        if row.get("url"):
            items.append(row)
    return items, ""


def parse_sitemap(text):
    try:
        root = ET.fromstring(text.encode("utf-8", "replace"))
    except ET.ParseError as exc:
        return [], [], "xml parse error: %s" % exc
    urls, children = [], []
    kind = _localname(root.tag)
    for node in root:
        loc, lastmod = "", ""
        for child in node:
            name = _localname(child.tag)
            if name == "loc":
                loc = (child.text or "").strip()
            elif name == "lastmod":
                lastmod = (child.text or "").strip()
        if not loc:
            continue
        if kind == "sitemapindex":
            children.append((loc, lastmod))
        else:
            urls.append({"url": loc, "posted": lastmod})
    return urls, children, ""


def parse_json_ld(text):
    """~20 lines of json.loads beats any LLM at reading a page that already
    carries structured data. Pages without it yield nothing on purpose."""
    blocks = []
    for match in re.finditer(
            r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', text,
            re.I | re.S):
        chunk = match.group(1).strip()
        if not chunk or len(chunk) > 400000:
            continue
        try:
            parsed = json.loads(chunk)
        except ValueError:
            continue
        # Descend through every container, not just @graph. Real listing pages
        # nest the listings: SearchResultsPage.mainEntity is an ItemList whose
        # itemListElement is a list of ListItem, each wrapping the listing in
        # "item". A top-level-only walk finds zero listings on such a page and
        # reports EMPTY-GENUINE - a silent false negative, the exact failure
        # this pipeline must never produce. Bounded so a hostile or huge
        # document cannot blow up the run.
        stack = [(parsed, 0)]
        seen = 0
        while stack and seen < MAX_LD_NODES:
            node, depth = stack.pop()
            if depth > MAX_LD_DEPTH:
                continue
            if isinstance(node, list):
                stack.extend((child, depth + 1) for child in node)
            elif isinstance(node, dict):
                seen += 1
                blocks.append(node)
                for value in node.values():
                    if isinstance(value, (dict, list)):
                        stack.append((value, depth + 1))
    return blocks


LISTING_TYPES = ("realestatelisting", "apartment", "house", "singlefamilyresidence",
                 "accommodation", "offer", "product", "residence", "suite")


def _ld_scalar(value):
    """JSON-LD fields are often objects. str() on a dict yields a fake id."""
    if isinstance(value, dict):
        inner = value.get("value") or value.get("@value") or ""
        return "" if isinstance(inner, (dict, list)) else str(inner or "")
    if isinstance(value, list):
        for item in value:
            found = _ld_scalar(item)
            if found:
                return found
        return ""
    return "" if value is None else str(value)


def _ld_price(node):
    """Price from a listing's nested Offer, or from an Offer node in its own right.

    A page that publishes the Offer as a sibling node - the common shape - used
    to lose the currency here, so one listing merged to "2400" and another to
    "USD 2400" for the same rent.
    """
    offers = node.get("offers")
    if isinstance(offers, list) and offers:
        offers = offers[0]
    source = offers if isinstance(offers, dict) else node
    price = _ld_scalar(source.get("price")) or _ld_scalar(source.get("lowPrice"))
    if not price:
        return ""
    return ("%s %s" % (_ld_scalar(source.get("priceCurrency")), price)).strip()


def _ld_address(node):
    address = node.get("address")
    if isinstance(address, dict):
        parts = [address.get(k) for k in
                 ("streetAddress", "addressLocality", "addressRegion", "postalCode")]
        return ", ".join(p for p in parts if p)
    return str(address or node.get("areaServed") or "")


def _merge_fragments(rows, page_url):
    """One listing arrives as several JSON-LD nodes - the Apartment carries the
    address, its nested Offer carries the price, an ApartmentComplex may carry
    the name. Walking the whole graph finds all of them, so they must be sewn
    back together or one home becomes three half-empty leads.

    Identity is the detail URL when there is one, else the title. Rows keyed to
    the page URL itself are fragments of whatever listing they sat inside, so
    they merge by title rather than becoming their own record.
    """
    merged = {}
    order = []
    for row in rows:
        url = row.get("url") or ""
        key = url if (url and url != page_url) else ("title:" + (row.get("title") or ""))
        if not key.strip() or key == "title:":
            continue
        if key not in merged:
            merged[key] = dict(row)
            order.append(key)
            continue
        target = merged[key]
        for field, value in row.items():
            if value and not target.get(field):
                target[field] = value
    # A title-keyed fragment is redundant once a URL-keyed row has the same title.
    titled = set(merged[k].get("title") for k in order if not k.startswith("title:"))
    out = []
    for key in order:
        if key.startswith("title:") and merged[key].get("title") in titled:
            continue
        out.append(merged[key])
    return out


def parse_html(text, page_url):
    """Structured data only. Raw page text never becomes a record."""
    rows = []
    for node in parse_json_ld(text):
        types = node.get("@type") or ""
        types = [types] if isinstance(types, str) else [t for t in types if isinstance(t, str)]
        if not any(str(t).lower() in LISTING_TYPES for t in types):
            continue
        rows.append({
            "url": str(node.get("url") or node.get("@id") or page_url),
            "title": str(node.get("name") or node.get("headline") or ""),
            "description": str(node.get("description") or ""),
            "price": _ld_price(node),
            "where": _ld_address(node),
            "posted": str(node.get("datePosted") or node.get("datePublished") or ""),
            "native_id": (_ld_scalar(node.get("identifier"))
                          or _ld_scalar(node.get("sku"))),
            # Kept beside the row so id_rule can name a specific JSON-LD field
            # (jsonld:@id) instead of guessing at whatever landed in native_id.
            "jsonld": {"@id": _ld_scalar(node.get("@id")),
                       "identifier": _ld_scalar(node.get("identifier")),
                       "sku": _ld_scalar(node.get("sku")),
                       "url": _ld_scalar(node.get("url"))},
        })
    if rows:
        return _merge_fragments(rows, page_url), "json-ld"
    meta = {}
    for match in re.finditer(
            r'<meta[^>]+(?:property|name)=["\'](og:[a-z:]+)["\'][^>]+content=["\']([^"\']{0,600})',
            text, re.I):
        meta[match.group(1).lower()] = match.group(2)
    if meta.get("og:title"):
        rows.append({"url": meta.get("og:url") or page_url, "title": meta["og:title"],
                     "description": meta.get("og:description", ""), "price": "",
                     "where": "", "posted": "", "native_id": ""})
        return rows, "og"
    micro = re.findall(r'itemprop=["\']name["\'][^>]*content=["\']([^"\']{0,200})', text, re.I)
    for name in micro[:MAX_RECORDS]:
        rows.append({"url": page_url, "title": name, "description": "", "price": "",
                     "where": "", "posted": "", "native_id": ""})
    return rows, ("microdata" if rows else "none")


def rows_for_channel(channel, text, page_url, source=None):
    """Parse one fetched body the way its channel says to. Returns (rows, shape, error).

    Probe and extract both call this, so a source that probes as usable parses
    the same way when the runtime fetches it. They used to disagree: probe read
    every body as HTML, which reported a healthy JSON API as EMPTY-GENUINE.
    """
    if channel == "rss":
        rows, error = parse_rss(text)
        return rows, "rss", error
    if channel == "sitemap":
        rows, _children, error = parse_sitemap(text)
        return rows, "sitemap", error
    if channel == "json":
        rows, error = parse_json_api(text, source or {})
        return rows, "json", error
    rows, shape = parse_html(text, page_url)
    return rows, shape, ""


COVERAGE_FIELDS = ("title", "url", "price", "where")


def field_coverage(rows):
    """How many parsed rows carry each field, and how many are usable as a lead.

    "Usable" means title and URL: without a title there is nothing for a person
    to read, and without a URL there is nowhere for them to go. Price is counted
    and reported but not required - "price on application" is a real listing.
    """
    coverage = dict((field, 0) for field in COVERAGE_FIELDS)
    usable = 0
    for row in rows:
        present = set()
        for field in COVERAGE_FIELDS:
            if str(row.get(field) or "").strip():
                coverage[field] += 1
                present.add(field)
        if {"title", "url"} <= present:
            usable += 1
    return coverage, usable


def dig(blob, dotted):
    node = blob
    for part in (dotted or "").split("."):
        if not part:
            continue
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit():
            node = node[int(part)] if int(part) < len(node) else None
        else:
            return None
    return node


def parse_json_api(text, source):
    try:
        blob = json.loads(text)
    except ValueError as exc:
        return [], "json parse error: %s" % exc
    spec = source.get("json") or {}
    records = dig(blob, spec.get("record_path", "")) if spec.get("record_path") else blob
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list):
        return [], "record_path did not resolve to a list"
    fields = spec.get("fields") or {}
    rows = []
    for record in records[:500]:
        if not isinstance(record, dict):
            continue
        row = {}
        for target, path in fields.items():
            value = dig(record, path)
            if value is not None and not isinstance(value, (dict, list)):
                row[target] = value
        if row.get("url"):
            rows.append(row)
    return rows, ""


# --- record building ---------------------------------------------------------

RECORD_LIMITS = (("title", 120), ("text", 240), ("price", 32), ("where", 80))


def _id_from_value(value):
    """A native id routinely arrives as a permalink (RSS guid, JSON-LD @id).

    Sanitizing a whole URL against the id charset yields "https:example.test123",
    which is stable but unreadable and collides across paths. Take the segment
    that actually identifies the listing instead.
    """
    text = str(value or "").strip()
    if text.lower().startswith(("http://", "https://")):
        parts = urllib.parse.urlsplit(text)
        segments = [s for s in parts.path.split("/") if s]
        text = segments[-1] if segments else parts.query
    return re.sub(r"[^A-Za-z0-9_.:~-]", "", text)[:64]


def native_id_for(source, row):
    """Extract source_listing_id per the source's id_rule.

        path_segment:<i> / path:<i>   URL path segment; negative counts from the end
        query:<name>                  query parameter
        feed:guid / guid              the feed item's <guid> or <id>
        jsonld:@id|identifier|sku|url the JSON-LD node's own field
        kyero:<field>                 a named field of a Kyero v3 record
        reddit:fullname               the t3_ fullname
        (absent)                      whatever the parser called native_id

    A rule that finds nothing falls back to the parser's native_id, then to "".
    An empty id is deliberate: the server hashes the canonical URL rather than
    this worker inventing an identity that would fork the lead across workers.
    Unknown rules never reach here - load_sources rejects them.
    """
    rule = str(source.get("id_rule") or "").strip()
    name, _sep, arg = rule.partition(":")
    name = name.lower()
    url = str(row.get("url") or "")
    jsonld = row.get("jsonld") if isinstance(row.get("jsonld"), dict) else {}
    candidate = ""
    if name in ("path", "path_segment"):
        segments = [s for s in urllib.parse.urlsplit(url).path.split("/") if s]
        try:
            candidate = segments[int(arg)]
        except (ValueError, IndexError):
            candidate = ""
    elif name == "query":
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        candidate = (query.get(arg) or [""])[0]
    elif name == "jsonld":
        candidate = str(jsonld.get(arg) or "")
        if not candidate and arg == "url":
            candidate = url
    elif name == "kyero":
        candidate = str(row.get(arg) or "")
    elif name == "reddit":
        candidate = str(row.get("fullname") or row.get("name") or "")
    # feed:guid, guid and "no rule at all" all mean: whatever the parser found.
    if not candidate:
        candidate = str(row.get("native_id") or "")
    return _id_from_value(candidate)


def canonical_url(url):
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query)
             if not k.lower().startswith(("utm_", "gclid", "fbclid", "mc_", "ref"))]
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc.lower(), path,
                                    urllib.parse.urlencode(query), ""))


def build_record(source, row, channel):
    url = canonical_url(str(row.get("url") or ""))
    native = native_id_for(source, row)
    text = row.get("description") or row.get("summary") or row.get("content") or ""
    record = {
        "id": hashlib.sha256(("%s\x1f%s" % (source["slug"], native or url))
                             .encode("utf-8")).hexdigest()[:16],
        "source": _clean(source["slug"], 40),
        "url": url[:300],
        "title": _clean(row.get("title") or row.get("name") or "", 120),
        "text": _clean(_strip_tags(str(text)), 240),
        "price": _clean(row.get("price") or row.get("price_display") or "", 32),
        "where": _clean(row.get("where") or row.get("location") or "", 80),
        "posted": _clean(row.get("posted") or "", 40),
        "channel": channel,
        "native_id": native,
    }
    if not _ISO.match(record["posted"]):
        record["posted"] = ""   # null rather than substituted: "posted 2h ago" is the value
    line = json.dumps(record, sort_keys=True)
    for field, floor in (("text", 0), ("title", 40), ("where", 0)):
        while len(line.encode("utf-8")) > MAX_RECORD_BYTES and len(record[field]) > floor:
            record[field] = record[field][:max(floor, len(record[field]) - 40)]
            line = json.dumps(record, sort_keys=True)
    if len(line.encode("utf-8")) > MAX_RECORD_BYTES:
        return None
    return record


# --- subcommands -------------------------------------------------------------


def cmd_probe(args):
    config = load_sources(args.sources) if args.sources else None
    allowed = config["allowed_hosts"] if config else \
        [(urllib.parse.urlsplit(args.url).hostname or "").lower()]
    if config:
        ok, reason = host_allowed(config, args.url)
        if not ok:
            die(EXIT_USAGE, "refuse: %s" % reason)
    elif not args.install_probe:
        die(EXIT_USAGE, "refuse: pass --sources, or --install-probe when a person is present")

    source = None
    for candidate in (config.get("sources") if config else []) or []:
        if candidate.get("slug") == args.slug:
            source = candidate
            break
    channel = args.channel or (source or {}).get("channel", "")

    record = {"slug": args.slug, "url_template": args.url, "egress_class": args.egress_class,
              "channel": channel, "vendor": "", "permitted_by": "",
              "checked_at": iso()}
    allows, delay, sitemaps, robots_status = check_robots(args.url, allowed)
    record["robots_status"] = robots_status
    record["crawl_delay"] = delay
    record["sitemaps"] = sitemaps[:10]
    if robots_status != 200:
        # A 5xx, a network error, or a 200 that is not text/plain. None of those
        # is the publisher answering, so none of them is a verdict on the source.
        record["status"] = "ROBOTS-UNAVAILABLE"
        record["report_as"] = "source_unchecked"
        record["reason"] = ("robots.txt did not answer (status %s); this is temporary, "
                            "re-probe rather than retire" % robots_status)
        emit(record)
        return
    if not allows:
        record["status"] = "ROBOTS-DISALLOWED"
        record["report_as"] = "source_unchecked"
        emit(record)
        return
    record["permitted_by"] = "robots:%s" % UA_TOKEN

    llms = http_get("%s://%s/llms.txt" % tuple(urllib.parse.urlsplit(args.url)[:2]),
                    allowed, max_bytes=64 * 1024)
    record["llms_txt"] = llms["status"] == 200

    time.sleep(max(delay, DEFAULT_INTERVAL))
    result = http_get(args.url, allowed)
    if result["error"]:
        record["status"] = "NETWORK-ERROR"
        record["report_as"] = "source_unchecked"
        record["reason"] = result["error"][:200]
        emit(record)
        return
    result["listings"] = count_listings(result, {})
    # The raw node count double-counts: one home contributes an Apartment, its
    # nested Offer, and sometimes an ApartmentComplex. Report what a run would
    # actually yield as leads, so a probe and a fetch agree on the number.
    ctype = (result.get("content_type") or "")
    if not channel and ctype.startswith("application/json"):
        channel = "json"
        record["channel"] = channel
    rows, shape, parse_error = [], "", ""
    if channel or not ctype or ctype.startswith("text/html"):
        rows, shape, parse_error = rows_for_channel(
            channel or "html", result["text"], args.url, source)
        if channel in ("", "html"):
            rows = [r for r in rows
                    if r.get("title") and str(r.get("url") or "").startswith("http")]
        if rows:
            record["listing_nodes"] = result["listings"]
            result["listings"] = len(rows)
        record["shape"] = shape
        if parse_error:
            record["parse_error"] = parse_error[:200]
    coverage, usable = field_coverage(rows)
    record["fields_present"] = coverage
    record["usable_records"] = usable
    if channel == "sitemap":
        # A sitemap says where the listings are, not what they say.
        record["record_shape"] = "discovery-only"
    if args.samples:
        record["samples"] = [
            {"title": _clean(row.get("title"), 120), "url": str(row.get("url") or "")[:300],
             "price": _clean(row.get("price"), 32), "where": _clean(row.get("where"), 80)}
            for row in rows[:max(0, min(int(args.samples), 3))]]
    classification, vendor = classify(result, {}, {"robots_allows": allows})
    record.update({"status": classification, "vendor": vendor, "bytes": result["bytes"],
                   "http_status": result["status"], "content_type": result["content_type"],
                   "final_url": result["final_url"], "listings": result["listings"],
                   "report_as": report_as(classification, result["listings"])})
    if classification in BLOCK_FAMILY:
        # Truncated hard, and never into a model's context.
        record["excerpt"] = _clean(_strip_tags(result["text"])[:200], 200)
    emit(record)


def count_listings(result, fingerprint, channel="", source=None):
    """How many listings this body actually holds.

    A calibrated listing_selector wins. Failing that, the source's channel is
    the answer: counting JSON-LD <script> blocks in a JSON API's response finds
    zero and reports a working API as EMPTY-GENUINE.
    """
    selector = (fingerprint or {}).get("listing_selector") or ""
    text = result.get("text") or ""
    if selector.startswith("ld+json:"):
        wanted = selector.split(":", 1)[1].lower()
        return sum(1 for node in parse_json_ld(text)
                   if wanted in str(node.get("@type", "")).lower())
    if selector == "rss:item":
        return len(re.findall(r"<(?:item|entry)[\s>]", text, re.I))
    if selector == "sitemap:url":
        return len(re.findall(r"<url[\s>]", text, re.I))
    if selector.startswith("substring:"):
        return text.lower().count(selector.split(":", 1)[1].lower())
    if channel:
        rows, _shape, _error = rows_for_channel(
            channel, text, result.get("final_url") or result.get("url") or "", source)
        return len(rows)
    if result.get("content_type", "").endswith("xml") or text.lstrip().startswith("<?xml"):
        return len(re.findall(r"<(?:item|entry|url)[\s>]", text, re.I))
    return len([n for n in parse_json_ld(text)
                if str(n.get("@type", "")).lower() in LISTING_TYPES])


def cmd_calibrate(args):
    config = load_sources(args.sources) if args.sources else None
    allowed = config["allowed_hosts"] if config else \
        [(urllib.parse.urlsplit(args.url_populated).hostname or "").lower()]
    if config:
        for url in (args.url_populated, args.url_empty):
            ok, reason = host_allowed(config, url)
            if not ok:
                die(EXIT_USAGE, "refuse: %s" % reason)
    elif not args.install_probe:
        die(EXIT_USAGE, "refuse: pass --sources, or --install-probe when a person is present")

    populated = http_get(args.url_populated, allowed)
    time.sleep(DEFAULT_INTERVAL)
    empty = http_get(args.url_empty, allowed)
    for result in (populated, empty):
        if result["error"] or not (200 <= result["status"] < 300):
            die(EXIT_NETWORK, "calibration fetch failed (%s %s)"
                % (result["status"], result["error"][:120]))

    candidates = set()
    for result in (populated, empty):
        text = result["text"]
        for node in parse_json_ld(text):
            types = node.get("@type")
            if isinstance(types, str):
                candidates.add('"@type":"%s"' % types)
        match = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)', text, re.I)
        if match:
            parts = urllib.parse.urlsplit(match.group(1))
            candidates.add("%s://%s" % (parts.scheme, parts.netloc))
        match = re.search(r'property=["\']og:site_name["\'][^>]+content=["\']([^"\']{2,60})',
                          text, re.I)
        if match:
            candidates.add(match.group(1))
    both = sorted(marker for marker in candidates
                  if marker.lower() in populated["text"].lower()
                  and marker.lower() in empty["text"].lower())[:4]

    best, best_gap = "", -1
    for selector in ("ld+json:RealEstateListing", "ld+json:Offer", "ld+json:Product",
                     "rss:item", "sitemap:url"):
        gap = count_listings(populated, {"listing_selector": selector}) - \
            count_listings(empty, {"listing_selector": selector})
        if gap > best_gap:
            best, best_gap = selector, gap
    fingerprint = {"shell_markers": both, "listing_selector": best,
                   "min_ok_bytes": int(empty["bytes"] * 0.5)}
    emit({"slug": args.slug, "fingerprint": fingerprint,
          "populated": {"bytes": populated["bytes"],
                        "listings": count_listings(populated, fingerprint)},
          "empty": {"bytes": empty["bytes"], "listings": count_listings(empty, fingerprint)},
          "usable": bool(both) and best_gap > 0})


def meta_path_for(out_dir, slug):
    return os.path.join(out_dir, "%s.meta.json" % slug)


def write_meta(out_dir, slug, payload):
    """The fetch/extract handoff. It carries the final status, not a draft of it.

    This file used to be written before the status was decided, so extract read
    a status-less record and fell back to BLOCKED-UNKNOWN - a healthy 304 or a
    cooldown arrived at the person as "this source is blocked".
    """
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    path = meta_path_for(out_dir, slug)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
    return path


def cmd_fetch(args):
    config = load_sources(args.sources)
    source = find_source(config, args.slug)
    state = load_state(args.state)
    entry = source_state(state, args.slug)
    allowed = config["allowed_hosts"]
    out = {"slug": args.slug, "lane": source.get("lane", ""), "pages": [],
           "egress_class": args.egress_class, "state_reset": state["state_reset"]}

    ready, reason = eligible(entry, args.egress_class)
    if not ready:
        out.update({"status": "SKIPPED-COOLDOWN", "report_as": "source_unchecked",
                    "reason": reason, "source_status": entry.get("status", ""),
                    "next_eligible": entry.get("next_eligible", ""),
                    "consecutive_blocks": int(entry.get("consecutive_blocks", 0)),
                    "meta_file": meta_path_for(args.out_dir, args.slug)})
        # Extract still runs in the generated cycle; without metadata it would
        # have no way to tell "we deliberately skipped this" from "we failed".
        write_meta(args.out_dir, args.slug, out)
        emit(out)
        return
    entry["egress_class"] = args.egress_class

    url = source.get("url_template") or ""
    ok, reason = host_allowed(config, url)
    if not ok:
        die(EXIT_USAGE, "refuse: %s" % reason)

    if now() - float(entry.get("robots_checked_at") or 0) > ROBOTS_TTL:
        allows, delay, _sitemaps, robots_status = check_robots(url, allowed)
        entry.update({"robots_allows": bool(allows), "crawl_delay": delay,
                      "robots_checked_at": now()})
        if robots_status != 200:
            # A 5xx, a network error, or a 200 that is not text/plain (a challenge
            # interstitial wearing robots.txt's URL). The publisher has not spoken,
            # so this buys a cooldown and re-probe - never a retirement.
            entry["robots_checked_at"] = 0        # do not cache a non-answer
            entry["status"] = "cooldown"
            entry["next_eligible"] = iso(now() + ROBOTS_COOLDOWN_SECONDS)
            entry["last_checked"] = iso()
            save_state(args.state, state)
            out.update({"status": "ROBOTS-UNAVAILABLE", "report_as": "source_unchecked",
                        "robots_status": robots_status,
                        "reason": "robots.txt for %s did not answer (status %s); cooling down "
                                  "until %s" % (url, robots_status, entry["next_eligible"]),
                        "source_status": entry["status"],
                        "next_eligible": entry["next_eligible"],
                        "consecutive_blocks": int(entry.get("consecutive_blocks", 0)),
                        "meta_file": meta_path_for(args.out_dir, args.slug)})
            write_meta(args.out_dir, args.slug, out)
            emit(out)
            return
        if not allows:
            # A retrievable robots.txt that disallows the path is the publisher's
            # own durable answer. This is the one non-negotiable retirement.
            entry["status"] = "retired"
            entry["next_eligible"] = ""
            entry["last_checked"] = iso()
            save_state(args.state, state)
            out.update({"status": "ROBOTS-DISALLOWED", "report_as": "source_unchecked",
                        "robots_status": robots_status,
                        "reason": "robots.txt disallows %s for %s" % (url, UA_TOKEN),
                        "source_status": entry["status"],
                        "meta_file": meta_path_for(args.out_dir, args.slug)})
            write_meta(args.out_dir, args.slug, out)
            emit(out)
            die(EXIT_CONSENT, "robots.txt disallows %s for %s" % (url, UA_TOKEN))

    interval = max(float(source.get("min_interval_seconds") or DEFAULT_INTERVAL),
                   float(entry.get("crawl_delay") or 0))
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    os.makedirs(args.out_dir, mode=0o700, exist_ok=True)

    targets = [url]
    fingerprint = source.get("fingerprint") or {}
    classification, vendor, index = "BLOCKED-UNKNOWN", "", 0
    max_pages = max(1, min(int(source.get("max_pages") or MAX_PAGES), MAX_PAGES))

    while targets and index < max_pages:
        target = targets.pop(0)
        wait_for_host(state, host, interval)
        etag = entry.get("etag", "") if index == 0 else ""
        since = entry.get("last_modified", "") if index == 0 else ""
        # Do not download a multi-megabyte HTML document only to reject it.
        # Complete listing data inside the bounded prefix is still usable;
        # classify() keeps a truncated zero from becoming EMPTY-GENUINE.
        fetch_cap = MAX_PAGE_BYTES if source["channel"] == "html" else None
        result = http_get(target, allowed, etag=etag, last_modified=since,
                          max_bytes=fetch_cap)
        if result["error"]:
            # One retry, and only for a bare network error.
            wait_for_host(state, host, interval)
            result = http_get(target, allowed, max_bytes=fetch_cap)
        if result["error"]:
            classification, vendor = "NETWORK-ERROR", ""
            out["pages"].append({"url": target, "status": 0, "error": result["error"][:200]})
            break

        result["listings"] = count_listings(result, fingerprint, source["channel"], source)
        classification, vendor = classify(result, fingerprint, entry)
        page = {"url": target, "final_url": result["final_url"], "status": result["status"],
                "bytes": result["bytes"], "classification": classification, "vendor": vendor,
                "content_type": result["content_type"], "listings": result["listings"]}

        if classification == "NOT-MODIFIED":
            out["pages"].append(page)
            break
        if source["channel"] == "html" and result["bytes"] > MAX_PAGE_BYTES:
            page["classification"] = classification = "SKIPPED-OVERSIZE"
            out["pages"].append(page)
            break
        if classification in BLOCK_FAMILY:
            page["excerpt"] = _clean(_strip_tags(result["text"])[:200], 200)
            out["pages"].append(page)
            break

        name = os.path.join(args.out_dir, "%s-%d.body" % (args.slug, index))
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(result["body"])
        page["file"] = name
        out["pages"].append(page)

        if index == 0:
            entry["etag"] = result["headers"].get("ETag", "")[:256]
            entry["last_modified"] = result["headers"].get("Last-Modified", "")[:256]
            entry["body_hash"] = result["body_hash"]
            entry["last_url"] = target[:256]
        if source["channel"] == "sitemap":
            _urls, children, _err = parse_sitemap(result["text"])
            for child_url, _lastmod in children:
                allowed_child, _reason = host_allowed(config, child_url)
                if allowed_child and len(targets) + index + 1 < max_pages:
                    targets.append(child_url)
        index += 1

    apply_retirement(entry, classification)
    entry["last_checked"] = iso()
    entry["last_fetch_at"] = now()
    save_state(args.state, state)

    out.update({"status": classification, "vendor": vendor,
                "report_as": report_as(classification,
                                       sum(p.get("listings", 0) for p in out["pages"])),
                "consecutive_blocks": entry["consecutive_blocks"],
                "source_status": entry["status"],
                "next_eligible": entry.get("next_eligible", ""),
                "channel": source["channel"],
                "meta_file": meta_path_for(args.out_dir, args.slug)})
    write_meta(args.out_dir, args.slug, out)
    emit(out)


def revalidate(config, source, url, allowed, state, interval):
    """Stale and delisted URLs were a real defect. Cheapest check first, stop at
    the first confident answer."""
    ok, reason = content_url_allowed(config, source, url)
    if not ok:
        return "unchecked", reason
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    wait_for_host(state, host, interval)
    result = http_get(url, allowed, method="HEAD", max_bytes=4096)
    if result["status"] in (404, 410):
        return "gone", "404/410"
    if result["status"] in (405, 501, 0) or result["status"] >= 500:
        wait_for_host(state, host, interval)
        result = http_get(url, allowed, max_bytes=MAX_PAGE_BYTES)
        if result["status"] in (404, 410):
            return "gone", "404/410"
    if _path_family(result["final_url"]) != _path_family(url):
        return "gone", "redirected out of the listing path family (soft-404)"
    if result.get("text") and DEAD_MARKERS.search(_strip_tags(result["text"])[:20000]):
        return "gone", "page says the listing is no longer available"
    if 200 <= result["status"] < 300:
        return "live", ""
    return "unchecked", "status %s" % result["status"]


def cmd_extract(args):
    config = load_sources(args.sources)
    source = find_source(config, args.slug)
    state = load_state(args.state)
    entry = source_state(state, args.slug)
    allowed = config["allowed_hosts"]
    interval = max(float(source.get("min_interval_seconds") or DEFAULT_INTERVAL),
                   float(entry.get("crawl_delay") or 0))

    meta_path = meta_path_for(args.in_dir, args.slug)
    try:
        with open(meta_path) as handle:
            meta = json.load(handle)
    except (OSError, ValueError):
        die(EXIT_USAGE, "no fetch metadata at %s; run fetch first" % meta_path)

    channel = source["channel"]
    if args.revalidate and not str(source.get("listing_url_pattern") or "").strip():
        # Revalidation compares the final URL against this pattern. Without it
        # every check returned "unchecked" and the run reported leads as
        # revalidated that had never been checked at all. Say so instead.
        die(EXIT_SCHEMA,
            "source %s has no listing_url_pattern, so revalidation cannot check anything; "
            "add the pattern to sources.json or pass --no-revalidate" % args.slug)

    counts = dict((k, 0) for k in ("parsed", "new", "duplicate", "stale", "urls_refused",
                                   "gone", "revalidate_unchecked", "dropped_oversize",
                                   "dropped_cap", "no_structured_data"))
    rows, parse_error = [], ""
    for page in meta.get("pages") or []:
        path = page.get("file")
        if not path or not os.path.exists(path):
            continue
        with open(path, "rb") as handle:
            text = handle.read(MAX_FETCH_BYTES).decode("utf-8", "replace")
        page_rows, shape, page_error = rows_for_channel(
            channel, text, page.get("final_url") or page.get("url", ""), source)
        if page_error and not parse_error:
            parse_error = page_error
        if channel == "html" and shape == "none":
            counts["no_structured_data"] += 1
        rows.extend(page_rows)
    counts["parsed"] = len(rows)

    cursor = entry.get("cursor", "")
    floor = ""
    if cursor and _ISO.match(cursor):
        floor = iso(max(0, _epoch(cursor) - OVERLAP_SECONDS))   # overlap, do not abut

    existing = 0
    if os.path.exists(args.out):
        with open(args.out) as handle:
            existing = sum(1 for line in handle if line.strip())
    room = max(0, min(args.max_records, MAX_RECORDS) - existing)

    seen = list(entry.get("seen_ids") or [])
    seen_set = set(seen)
    records, revalidations, newest = [], 0, cursor
    for row in rows:
        if len(records) >= room:
            counts["dropped_cap"] += 1
            continue
        url = str(row.get("url") or "")
        ok, _reason = host_allowed(config, url)
        if not ok:
            counts["urls_refused"] += 1
            continue
        posted = str(row.get("posted") or "")
        if floor and _ISO.match(posted) and posted < floor:
            counts["stale"] += 1      # older than the cursor: healthy, not missing
            continue
        record = build_record(source, row, channel)
        if record is None:
            counts["dropped_oversize"] += 1
            continue
        key = record["native_id"] or record["url"]
        if key in seen_set:
            counts["duplicate"] += 1
            continue
        if args.revalidate and revalidations < MAX_REVALIDATIONS:
            revalidations += 1
            verdict, _why = revalidate(config, source, record["url"], allowed, state, interval)
            if verdict == "gone":
                counts["gone"] += 1
                continue
            if verdict == "unchecked":
                counts["revalidate_unchecked"] += 1
        records.append(record)
        seen_set.add(key)
        seen.append(key)
        counts["new"] += 1
        if _ISO.match(record["posted"]) and record["posted"] > (newest or ""):
            newest = record["posted"]

    os.makedirs(os.path.dirname(args.out) or ".", mode=0o700, exist_ok=True)
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    entry["seen_ids"] = seen[-MAX_SEEN_IDS:]
    if newest:
        entry["cursor"] = newest
    save_state(args.state, state)

    classification = extract_status(meta, counts)
    emit({"slug": args.slug, "lane": source.get("lane", ""), "counts": counts,
          "records_written": len(records), "candidates_file": args.out,
          "cursor": entry.get("cursor", ""), "parse_error": parse_error[:200],
          "channel": channel, "fetch_status": fetch_status(meta),
          "source_status": entry.get("status", ""),
          # A sitemap says where the listings are, not what they say: these rows
          # carry a URL and a date, and no detail page is fetched to fill in a
          # title, a price or a location.
          "record_shape": "discovery-only" if channel == "sitemap" else "full",
          "status": classification,
          "report_as": extract_report_as(classification, counts)})


def fetch_status(meta):
    """What the fetch decided, or the best reconstruction of it.

    The fallback matters: a metadata file written by an older build has no
    status, and guessing "blocked" there is exactly the lie this pipeline is
    built to avoid. An unknown status is SOURCE-UNCHECKED, which is transient.
    """
    status = str(meta.get("status") or "").strip()
    if status:
        return status
    for page in reversed(meta.get("pages") or []):
        if page.get("classification"):
            return str(page["classification"])
    return "SOURCE-UNCHECKED"


def extract_status(meta, counts):
    """Carry the fetch's status forward; a block on any page still wins."""
    status = fetch_status(meta)
    for page in meta.get("pages") or []:
        if page.get("classification") in BLOCK_FAMILY:
            return str(page["classification"])
    if status in BLOCK_FAMILY or status in TRANSIENT_FAMILY or status == "NOT-MODIFIED":
        return status
    return "OK" if counts.get("new") else status


def extract_report_as(classification, counts):
    """What the person is told. Only a real zero may be reported as a zero."""
    if classification in BLOCK_FAMILY or classification in TRANSIENT_FAMILY:
        return "source_unchecked"
    if counts.get("new"):
        return "ok"
    if classification in HEALTHY_ZERO:
        return "nothing_new"
    if classification == "OK" and (counts.get("duplicate") or counts.get("stale")
                                   or counts.get("gone")):
        # The page came back healthy and every row on it was already seen, older
        # than the cursor, or confirmed delisted. That is a genuine "nothing new".
        return "nothing_new"
    return "source_unchecked"


def _epoch(value):
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(value[:len(time.strftime(fmt))], fmt))
        except ValueError:
            continue
    return 0


# --- CLI ---------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="sources.py",
        description="Fetch listing sources and extract bounded records. No key, no model.",
        epilog=("Requests present this machine's browser identity by default; --ua crawler "
                "sends the self-identified HomingAgent token instead, and robots.txt is "
                "evaluated against whichever group matches the identity actually sent. "
                "There is no CAPTCHA-solving, proxy-rotation, fingerprint-evasion or "
                "cookie-replay path anywhere in this file. Only hosts named in sources.json "
                "are fetched. EMPTY-GENUINE is the only zero reported as 'nothing new'; a "
                "transient failure reports as unchecked and cools down rather than retiring "
                "a source. The sitemap channel is discovery-only - URLs and dates, no detail "
                "pages. Exit codes are documented at the top of this file."))
    subparsers = parser.add_subparsers(dest="command")

    probe = subparsers.add_parser(
        "probe", help="classify one source's reachability",
        description="Classify one source: robots posture, reachability, and how many "
                    "records the configured channel actually yields.")
    probe.add_argument("--url", required=True)
    probe.add_argument("--slug", required=True)
    probe.add_argument("--channel", default="", choices=("",) + CHANNELS,
                       help="how to parse the body (default: this slug's channel in "
                            "--sources, else html)")
    probe.add_argument("--sources", default="")
    probe.add_argument("--install-probe", action="store_true",
                       help="allow a host that is not yet in sources.json (installer only)")
    probe.add_argument("--egress-class", default=os.environ.get("HOMING_EGRESS_CLASS", "unknown"))
    probe.add_argument("--ua", default="browser", choices=("browser", "crawler"),
                       help="which client identity to present (default: browser)")
    probe.add_argument("--samples", type=int, default=0, metavar="N",
                       help="include up to N (max 3) cleaned sample records in the output")

    cal = subparsers.add_parser("calibrate",
                                help="record the populated-vs-empty fingerprint for a source")
    cal.add_argument("--slug", required=True)
    cal.add_argument("--url-populated", required=True, help="a query known to return results")
    cal.add_argument("--url-empty", required=True, help="a query known to return none")
    cal.add_argument("--sources", default="")
    cal.add_argument("--install-probe", action="store_true")

    fetch = subparsers.add_parser("fetch", help="fetch one source's pages into a run directory")
    fetch.add_argument("--slug", required=True)
    fetch.add_argument("--sources", required=True)
    fetch.add_argument("--state", default=os.environ.get("HOMING_SOURCES_STATE", ""))
    fetch.add_argument("--out-dir", required=True)
    fetch.add_argument("--egress-class", default=os.environ.get("HOMING_EGRESS_CLASS", "unknown"))

    extract = subparsers.add_parser(
        "extract", help="turn fetched pages into bounded candidate records",
        description="Turn fetched pages into bounded candidate records, carrying the "
                    "fetch's status forward. A sitemap source is discovery-only: its "
                    "records carry a URL and a date, never a title, price or location, "
                    "because no detail page is fetched.")
    extract.add_argument("--slug", required=True)
    extract.add_argument("--sources", required=True)
    extract.add_argument("--state", default=os.environ.get("HOMING_SOURCES_STATE", ""))
    extract.add_argument("--in-dir", required=True)
    extract.add_argument("--out", required=True, help="candidates.jsonl to append to")
    extract.add_argument("--max-records", type=int, default=MAX_RECORDS)
    extract.add_argument("--revalidate", dest="revalidate", action="store_true", default=True,
                         help="check each listing is still live (default); requires the "
                              "source to define listing_url_pattern")
    extract.add_argument("--no-revalidate", dest="revalidate", action="store_false",
                         help="write records without checking they are still live")
    return parser


COMMANDS = {"probe": cmd_probe, "calibrate": cmd_calibrate,
            "fetch": cmd_fetch, "extract": cmd_extract}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_USAGE
    set_user_agent(getattr(args, "ua", "browser"))
    placeholder = "__" + "HOMING_ORIGIN" + "__"
    if placeholder in ORIGIN and args.command not in ("probe", "calibrate"):
        # probe/calibrate reach only the listing source, never Homing, so an
        # uninstalled copy can still be used to test a source by hand.
        die(EXIT_CONFIG, "this copy was never installed; the origin is still a placeholder")
    COMMANDS[args.command](args)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
