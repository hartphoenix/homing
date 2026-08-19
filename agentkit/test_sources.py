"""Deterministic tests for the source fetch/extract pipeline.

Nothing here touches the network. ``http_get`` is replaced by a fixture table,
so a run in CI cannot depend on a live site's robots policy, its edge vendor's
mood, or whether a listing was still up this morning. The one test that does
exercise the real transport swaps in a fake opener to prove the truncated-read
tolerance, which is the one behaviour a fixture table cannot express.

The assertions are about **classification**, not volume: "records came back" is
the easy half. The failure this pipeline exists to prevent is a healthy source
reported as blocked, or a block reported as "nothing new" - so every case below
pins the status and the reported outcome, not just the record count.
"""

import hashlib
import http.client
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest.mock as mock

from django.test import SimpleTestCase

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "package", "scripts")


def _load(name):
    """Load a packaged script by path; the kit ships them as scripts, not modules."""
    spec = importlib.util.spec_from_file_location("agentkit_%s" % name,
                                                  os.path.join(SCRIPTS, "%s.py" % name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sources = _load("sources")
verify = _load("verify_sources")
sources.ORIGIN = "https://homing.test"          # as the installer would have written it


# --- fixtures ----------------------------------------------------------------

NESTED_LD = """<!doctype html><html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"SearchResultsPage","mainEntity":{
 "@type":"ItemList","itemListElement":[
  {"@type":"ListItem","position":1,"item":{
    "@type":"Apartment","@id":"https://example.test/listing/1001",
    "name":"Sunny one bed","url":"https://example.test/listing/1001",
    "identifier":{"@type":"PropertyValue","value":"1001"},
    "description":"Top floor, quiet street.",
    "address":{"@type":"PostalAddress","streetAddress":"12 Elm St",
               "addressLocality":"Brooklyn"},
    "datePosted":"2026-08-15T09:00:00Z"}},
  {"@type":"ListItem","position":2,"item":{
    "@type":"Apartment","@id":"https://example.test/listing/1002",
    "name":"Garden studio","url":"https://example.test/listing/1002",
    "identifier":"1002","description":"Rear garden.",
    "address":{"@type":"PostalAddress","streetAddress":"3 Oak Rd"},
    "datePosted":"2026-08-16T09:00:00Z"}}]}}
</script>
<script type="application/ld+json">
{"@type":"Offer","url":"https://example.test/listing/1001",
 "price":"2400","priceCurrency":"USD"}
</script>
</head><body><h1>Rentals</h1></body></html>"""

EMPTY_PAGE = ("<!doctype html><html><head><title>Rentals</title></head>"
              "<body><p>No results matched your search.</p>" + ("<!-- pad -->" * 200) +
              "</body></html>")

CHALLENGE_PAGE = ("<!doctype html><html><head><title>Just a moment...</title></head>"
                  "<body>Enable JavaScript and cookies to continue</body></html>")

SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.test/listing/2001</loc><lastmod>2026-08-16</lastmod></url>
  <url><loc>https://example.test/listing/2002</loc><lastmod>2026-08-17</lastmod></url>
</urlset>"""

RSS_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel>
 <item><title>Loft in Bushwick</title>
  <link>https://example.test/listing/3001</link>
  <guid>https://example.test/listing/3001</guid>
  <description>Two bed loft</description>
  <pubDate>2026-08-16T10:00:00Z</pubDate></item>
</channel></rss>"""

API_JSON = json.dumps({"data": {"items": [
    {"id": "9001", "name": "Riverside flat", "link": "https://example.test/listing/9001",
     "rent": "$2,100", "hood": "Greenpoint"},
    {"id": "9002", "name": "Corner studio", "link": "https://example.test/listing/9002",
     "rent": "$1,800", "hood": "Ridgewood"}]}})

ROBOTS_ALLOW = "User-agent: *\nAllow: /\n"
ROBOTS_DENY = "User-agent: *\nDisallow: /rent\n"

ROBOTS_URL = "https://example.test/robots.txt"
PAGE_URL = "https://example.test/rent"


def result(url, status=200, body="", headers=None, content_type="text/html",
           error="", final_url=None):
    """The shape http_get returns, built without a socket."""
    raw = body.encode("utf-8") if isinstance(body, str) else body
    if error:
        return {"url": url, "final_url": url, "status": 0, "headers": {}, "bytes": 0,
                "text": "", "body": b"", "content_type": "", "body_hash": "",
                "error": error}
    head = dict(headers or {})
    if content_type:
        head.setdefault("Content-Type", content_type)
    return {"url": url, "final_url": final_url or url, "status": status, "headers": head,
            "bytes": len(raw), "text": raw.decode("utf-8", "replace"), "body": raw,
            "content_type": content_type, "truncated": False,
            "body_hash": hashlib.sha256(raw).hexdigest()[:32], "error": ""}


class FakeNet:
    """A fixture table standing in for http_get. Unlisted URLs are a test bug."""

    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls = []

    def __call__(self, url, allowed_hosts, etag="", last_modified="", method="GET",
                 max_bytes=None):
        self.calls.append({"url": url, "method": method, "etag": etag,
                           "max_bytes": max_bytes})
        handler = self.routes.get(url)
        if handler is None:
            raise AssertionError("test fetched an unrouted URL: %s" % url)
        if callable(handler):
            return handler(url, etag, last_modified, method)
        return handler


def robots_ok(body=ROBOTS_ALLOW):
    return result(ROBOTS_URL, body=body, content_type="text/plain")


class PipelineCase(SimpleTestCase):
    """Temp run directory, a sources.json, and a CLI runner that captures stdout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.raw = os.path.join(self.dir, "raw")
        self.out = os.path.join(self.dir, "candidates.jsonl")
        self.state = os.path.join(self.dir, "state.json")

    def write_sources(self, **overrides):
        source = {"slug": "example-test", "channel": "html",
                  "lane": "example-test:html", "url_template": PAGE_URL,
                  "listing_url_pattern": r"^https://example\.test/listing/",
                  "id_rule": "path_segment:-1"}
        source.update(overrides)
        config = {"allowed_hosts": ["example.test"], "sources": [source]}
        path = os.path.join(self.dir, "sources.json")
        with open(path, "w") as handle:
            json.dump(config, handle)
        return path

    def write_state(self, **entry):
        base = sources.blank_source_state()
        base.update(entry)
        with open(self.state, "w") as handle:
            json.dump({"protocol": 1, "sources": {"example-test": base}, "hosts": {}},
                      handle)

    def read_state(self):
        with open(self.state) as handle:
            return json.load(handle)["sources"]["example-test"]

    def run_cli(self, argv, net):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sources, "http_get", net), \
                mock.patch.object(sys, "stdout", stdout), \
                mock.patch.object(sys, "stderr", stderr), \
                mock.patch.object(time, "sleep", lambda *a, **k: None):
            try:
                code = sources.main(argv)
            except SystemExit as exc:
                code = exc.code if exc.code is not None else 0
        emitted = [json.loads(line) for line in stdout.getvalue().splitlines()
                   if line.strip()]
        return code, emitted, stderr.getvalue()

    def fetch(self, net, sources_path=None, extra=()):
        return self.run_cli(["fetch", "--slug", "example-test",
                             "--sources", sources_path or self.write_sources(),
                             "--state", self.state, "--out-dir", self.raw] + list(extra),
                            net)

    def extract(self, net, sources_path=None, extra=()):
        return self.run_cli(["extract", "--slug", "example-test",
                             "--sources", sources_path or self.write_sources(),
                             "--state", self.state, "--in-dir", self.raw,
                             "--out", self.out] + list(extra), net)

    def records(self):
        if not os.path.exists(self.out):
            return []
        with open(self.out) as handle:
            return [json.loads(line) for line in handle if line.strip()]


# --- parsing -----------------------------------------------------------------


class ParserTests(SimpleTestCase):

    def test_nested_json_ld_is_walked_and_fragments_merged(self):
        rows, shape = sources.parse_html(NESTED_LD, PAGE_URL)
        self.assertEqual(shape, "json-ld")
        by_url = dict((row["url"], row) for row in rows)
        self.assertEqual(sorted(by_url), ["https://example.test/listing/1001",
                                          "https://example.test/listing/1002"])
        first = by_url["https://example.test/listing/1001"]
        self.assertEqual(first["title"], "Sunny one bed")
        self.assertEqual(first["where"], "12 Elm St, Brooklyn")
        # The price lives in a separate Offer node keyed to the same URL.
        self.assertEqual(first["price"], "USD 2400")
        self.assertEqual(first["jsonld"]["@id"], "https://example.test/listing/1001")

    def test_property_value_identifier_does_not_stringify_the_dict(self):
        rows, _shape = sources.parse_html(NESTED_LD, PAGE_URL)
        first = [r for r in rows if r["url"].endswith("1001")][0]
        self.assertEqual(first["native_id"], "1001")

    def test_merge_fragments_keeps_one_row_per_listing(self):
        merged = sources._merge_fragments([
            {"url": PAGE_URL, "title": "Sunny one bed", "price": "USD 2400"},
            {"url": "https://example.test/listing/1001", "title": "Sunny one bed"},
        ], PAGE_URL)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["url"], "https://example.test/listing/1001")

    def test_rows_for_channel_dispatches_by_channel(self):
        rows, shape, error = sources.rows_for_channel("rss", RSS_XML, PAGE_URL)
        self.assertEqual((shape, error), ("rss", ""))
        self.assertEqual(rows[0]["url"], "https://example.test/listing/3001")
        rows, shape, _ = sources.rows_for_channel("sitemap", SITEMAP_XML, PAGE_URL)
        self.assertEqual((shape, len(rows)), ("sitemap", 2))
        spec = {"json": {"record_path": "data.items",
                         "fields": {"url": "link", "title": "name", "native_id": "id"}}}
        rows, shape, _ = sources.rows_for_channel("json", API_JSON, PAGE_URL, spec)
        self.assertEqual((shape, len(rows)), ("json", 2))

    def test_json_api_parse_error_is_reported_not_swallowed(self):
        rows, _shape, error = sources.rows_for_channel("json", "{oops", PAGE_URL, {})
        self.assertEqual(rows, [])
        self.assertIn("json parse error", error)

    def test_field_coverage_requires_title_and_url_to_call_a_row_usable(self):
        coverage, usable = sources.field_coverage([
            {"title": "A", "url": "https://example.test/listing/1", "price": "$1"},
            {"title": "", "url": "https://example.test/listing/2"},
            {"title": "C", "url": ""},
        ])
        self.assertEqual(usable, 1)
        self.assertEqual(coverage, {"title": 2, "url": 2, "price": 1, "where": 0})


class TruncatedReadTests(SimpleTestCase):
    """The real transport, with a socket that ends short. Measured on live pages."""

    def test_incomplete_read_keeps_the_bytes_that_arrived(self):
        partial = b"<html>most of a listing page</html>"

        class _Response:
            status = 200
            headers = {"Content-Type": "text/html"}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def geturl(self):
                return PAGE_URL

            def read(self, _size):
                raise http.client.IncompleteRead(partial)

        class _Opener:
            addheaders = []

            def open(self, _request, timeout=None):
                return _Response()

        with mock.patch.object(sources.urllib.request, "build_opener",
                               lambda *a, **k: _Opener()):
            got = sources.http_get(PAGE_URL, ["example.test"])
        self.assertEqual(got["error"], "")
        self.assertEqual(got["status"], 200)
        self.assertEqual(got["body"], partial)


# --- schema ------------------------------------------------------------------


class SchemaTests(PipelineCase):

    def load(self, **overrides):
        path = self.write_sources(**overrides)
        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr):
            try:
                return sources.load_sources(path), 0, stderr.getvalue()
            except SystemExit as exc:
                return None, exc.code, stderr.getvalue()

    def test_valid_source_loads(self):
        config, code, _ = self.load()
        self.assertEqual(code, 0)
        self.assertEqual(config["sources"][0]["slug"], "example-test")

    def test_unknown_channel_is_rejected(self):
        _, code, message = self.load(channel="api")
        self.assertEqual(code, sources.EXIT_SCHEMA)
        self.assertIn("unknown channel", message)

    def test_unknown_id_rule_is_rejected(self):
        _, code, message = self.load(id_rule="magic:7")
        self.assertEqual(code, sources.EXIT_SCHEMA)
        self.assertIn("unknown id_rule", message)

    def test_invalid_listing_url_pattern_is_rejected(self):
        _, code, message = self.load(listing_url_pattern="([")
        self.assertEqual(code, sources.EXIT_SCHEMA)
        self.assertIn("invalid listing_url_pattern", message)

    def test_documented_id_rules_all_validate(self):
        for rule in ("path_segment:-1", "path:0", "query:id", "feed:guid", "guid",
                     "jsonld:@id", "jsonld:identifier", "kyero:id", "reddit:fullname", ""):
            self.assertTrue(sources.valid_id_rule(rule), rule)
        for rule in ("path_segment", "path_segment:last", "query:", "feed:permalink",
                     "jsonld:name", "sitemap:loc"):
            self.assertFalse(sources.valid_id_rule(rule), rule)


class NativeIdTests(SimpleTestCase):

    def id_for(self, rule, row):
        return sources.native_id_for({"id_rule": rule}, row)

    def test_path_segment_counts_from_the_end(self):
        row = {"url": "https://example.test/for-rent/dublin/6645832"}
        self.assertEqual(self.id_for("path_segment:-1", row), "6645832")
        self.assertEqual(self.id_for("path_segment:0", row), "for-rent")
        self.assertEqual(self.id_for("path:1", row), "dublin")

    def test_query_rule(self):
        row = {"url": "https://example.test/l?id=A-42&utm_source=x"}
        self.assertEqual(self.id_for("query:id", row), "A-42")

    def test_feed_guid_takes_the_identifying_segment_of_a_permalink(self):
        row = {"url": "https://example.test/listing/3001",
               "native_id": "https://example.test/listing/3001"}
        self.assertEqual(self.id_for("feed:guid", row), "3001")
        self.assertEqual(self.id_for("guid", row), "3001")

    def test_jsonld_at_id(self):
        row = {"url": "https://example.test/listing/1001",
               "jsonld": {"@id": "https://example.test/listing/1001",
                          "identifier": "1001", "sku": "SKU-7", "url": ""}}
        self.assertEqual(self.id_for("jsonld:@id", row), "1001")
        self.assertEqual(self.id_for("jsonld:identifier", row), "1001")
        self.assertEqual(self.id_for("jsonld:sku", row), "SKU-7")

    def test_kyero_and_reddit_rules(self):
        self.assertEqual(self.id_for("kyero:ref", {"ref": "ES-1234"}), "ES-1234")
        self.assertEqual(self.id_for("reddit:fullname", {"fullname": "t3_abc123"}),
                         "t3_abc123")

    def test_rule_that_finds_nothing_falls_back_then_gives_up(self):
        self.assertEqual(self.id_for("query:id", {"url": "https://example.test/l",
                                                 "native_id": "77"}), "77")
        self.assertEqual(self.id_for("query:id", {"url": "https://example.test/l"}), "")


# --- fetch: robots posture ---------------------------------------------------


class RobotsTests(PipelineCase):

    def test_disallow_from_a_retrievable_robots_retires_the_source(self):
        net = FakeNet({ROBOTS_URL: robots_ok(ROBOTS_DENY)})
        code, emitted, _ = self.fetch(net)
        self.assertEqual(code, sources.EXIT_CONSENT)
        self.assertEqual(emitted[-1]["status"], "ROBOTS-DISALLOWED")
        self.assertEqual(emitted[-1]["report_as"], "source_unchecked")
        self.assertEqual(self.read_state()["status"], "retired")

    def test_transient_5xx_cools_down_and_never_retires(self):
        net = FakeNet({ROBOTS_URL: result(ROBOTS_URL, status=503, body="oops")})
        code, emitted, _ = self.fetch(net)
        self.assertEqual(code, sources.EXIT_OK)
        self.assertEqual(emitted[-1]["status"], "ROBOTS-UNAVAILABLE")
        self.assertEqual(emitted[-1]["report_as"], "source_unchecked")
        entry = self.read_state()
        self.assertEqual(entry["status"], "cooldown")
        self.assertNotEqual(entry["next_eligible"], "")
        self.assertEqual(entry["consecutive_blocks"], 0)
        self.assertEqual(entry["robots_checked_at"], 0)   # a non-answer is not cached

    def test_network_error_on_robots_cools_down(self):
        net = FakeNet({ROBOTS_URL: result(ROBOTS_URL, error="connection reset")})
        code, emitted, _ = self.fetch(net)
        self.assertEqual(code, sources.EXIT_OK)
        self.assertEqual(emitted[-1]["status"], "ROBOTS-UNAVAILABLE")
        self.assertEqual(self.read_state()["status"], "cooldown")

    def test_challenge_html_served_as_robots_cools_down(self):
        net = FakeNet({ROBOTS_URL: result(ROBOTS_URL, body=CHALLENGE_PAGE,
                                          content_type="text/html")})
        code, emitted, _ = self.fetch(net)
        self.assertEqual(code, sources.EXIT_OK)
        self.assertEqual(emitted[-1]["status"], "ROBOTS-UNAVAILABLE")
        self.assertEqual(self.read_state()["status"], "cooldown")

    def test_4xx_robots_is_unavailable_so_the_fetch_proceeds(self):
        for status in (403, 404, 429):
            with self.subTest(status=status):
                self.setUp()
                net = FakeNet({ROBOTS_URL: result(ROBOTS_URL, status=status, body=""),
                               PAGE_URL: result(PAGE_URL, body=NESTED_LD)})
                code, emitted, _ = self.fetch(net)
                self.assertEqual(code, sources.EXIT_OK)
                self.assertEqual(emitted[-1]["status"], "OK")
                self.assertNotEqual(self.read_state()["status"], "retired")


# --- fetch and extract: status propagation -----------------------------------


class StatusPropagationTests(PipelineCase):

    def test_html_fetch_uses_bounded_prefix_when_it_contains_complete_records(self):
        page = result(PAGE_URL, body=NESTED_LD)
        page["truncated"] = True
        page["bytes"] = sources.MAX_PAGE_BYTES
        net = FakeNet({ROBOTS_URL: robots_ok(), PAGE_URL: page})
        code, emitted, _ = self.fetch(net)
        self.assertEqual(code, sources.EXIT_OK)
        self.assertEqual(emitted[-1]["status"], "OK")
        page_call = [call for call in net.calls if call["url"] == PAGE_URL][0]
        self.assertEqual(page_call["max_bytes"], sources.MAX_PAGE_BYTES)

        code, emitted, _ = self.extract(FakeNet({}), extra=["--no-revalidate"])
        self.assertEqual(code, sources.EXIT_OK)
        self.assertEqual(emitted[-1]["counts"]["parsed"], 2)

    def test_truncated_html_without_complete_records_is_not_genuine_empty(self):
        page = result(PAGE_URL, body="<html><script type='application/ld+json'>{")
        page["truncated"] = True
        page["bytes"] = sources.MAX_PAGE_BYTES
        net = FakeNet({ROBOTS_URL: robots_ok(), PAGE_URL: page})
        code, emitted, _ = self.fetch(net)
        self.assertEqual(code, sources.EXIT_OK)
        self.assertEqual(emitted[-1]["status"], "SKIPPED-OVERSIZE")
        self.assertEqual(emitted[-1]["report_as"], "source_unchecked")
        self.assertEqual(self.read_state()["status"], "cooldown")

    def test_not_modified_survives_the_handoff_to_extract(self):
        net = FakeNet({ROBOTS_URL: robots_ok(),
                       PAGE_URL: result(PAGE_URL, status=304, body="")})
        code, emitted, _ = self.fetch(net)
        self.assertEqual(code, sources.EXIT_OK)
        self.assertEqual(emitted[-1]["status"], "NOT-MODIFIED")
        self.assertEqual(emitted[-1]["report_as"], "nothing_new")

        with open(emitted[-1]["meta_file"]) as handle:
            self.assertEqual(json.load(handle)["status"], "NOT-MODIFIED")

        code, emitted, _ = self.extract(FakeNet({}))
        self.assertEqual(code, sources.EXIT_OK)
        self.assertEqual(emitted[-1]["status"], "NOT-MODIFIED")
        self.assertEqual(emitted[-1]["report_as"], "nothing_new")
        self.assertNotIn(emitted[-1]["status"], sources.BLOCK_FAMILY)
        self.assertEqual(self.read_state()["status"], "ok")

    def test_genuine_empty_is_reported_as_nothing_new_not_blocked(self):
        net = FakeNet({ROBOTS_URL: robots_ok(),
                       PAGE_URL: result(PAGE_URL, body=EMPTY_PAGE)})
        code, emitted, _ = self.fetch(net)
        self.assertEqual(emitted[-1]["status"], "EMPTY-GENUINE")
        code, emitted, _ = self.extract(FakeNet({}))
        self.assertEqual(emitted[-1]["status"], "EMPTY-GENUINE")
        self.assertEqual(emitted[-1]["report_as"], "nothing_new")
        self.assertNotIn(emitted[-1]["status"], sources.BLOCK_FAMILY)
        self.assertEqual(self.records(), [])

    def test_cooldown_skip_leaves_metadata_and_does_not_become_a_block(self):
        self.write_state(status="blocked", egress_class="residential",
                         next_eligible=sources.iso(time.time() + 86400),
                         consecutive_blocks=1)
        net = FakeNet({})
        code, emitted, _ = self.fetch(net, extra=["--egress-class", "residential"])
        self.assertEqual(code, sources.EXIT_OK)
        self.assertEqual(emitted[-1]["status"], "SKIPPED-COOLDOWN")
        self.assertTrue(os.path.exists(emitted[-1]["meta_file"]))

        code, emitted, _ = self.extract(FakeNet({}))
        self.assertEqual(emitted[-1]["status"], "SKIPPED-COOLDOWN")
        self.assertEqual(emitted[-1]["report_as"], "source_unchecked")
        self.assertNotIn(emitted[-1]["status"], sources.BLOCK_FAMILY)

    def test_challenge_page_is_a_block_and_advances_the_ladder_once(self):
        net = FakeNet({ROBOTS_URL: robots_ok(),
                       PAGE_URL: result(PAGE_URL, status=403, body=CHALLENGE_PAGE,
                                        headers={"cf-mitigated": "challenge",
                                                 "cf-ray": "1", "Server": "cloudflare"})})
        code, emitted, _ = self.fetch(net)
        self.assertEqual(code, sources.EXIT_OK)
        self.assertEqual(emitted[-1]["status"], "BLOCKED-EDGE")
        self.assertEqual(emitted[-1]["report_as"], "source_unchecked")
        entry = self.read_state()
        self.assertEqual((entry["status"], entry["consecutive_blocks"]), ("blocked", 1))

        code, emitted, _ = self.extract(FakeNet({}))
        self.assertEqual(emitted[-1]["status"], "BLOCKED-EDGE")
        self.assertEqual(emitted[-1]["report_as"], "source_unchecked")

    def test_network_error_cools_down_rather_than_reporting_the_source_healthy(self):
        net = FakeNet({ROBOTS_URL: robots_ok(),
                       PAGE_URL: result(PAGE_URL, error="timed out")})
        code, emitted, _ = self.fetch(net)
        self.assertEqual(emitted[-1]["status"], "NETWORK-ERROR")
        entry = self.read_state()
        self.assertEqual(entry["status"], "cooldown")
        self.assertEqual(entry["consecutive_blocks"], 0)

    def test_metadata_without_a_status_is_unchecked_not_blocked(self):
        os.makedirs(self.raw, mode=0o700, exist_ok=True)
        with open(sources.meta_path_for(self.raw, "example-test"), "w") as handle:
            json.dump({"slug": "example-test", "pages": []}, handle)
        code, emitted, _ = self.extract(FakeNet({}))
        self.assertEqual(emitted[-1]["status"], "SOURCE-UNCHECKED")
        self.assertEqual(emitted[-1]["report_as"], "source_unchecked")


# --- extract: records --------------------------------------------------------


class ExtractionTests(PipelineCase):

    def fetch_page(self, body, net_extra=None, **source_overrides):
        routes = {ROBOTS_URL: robots_ok(), PAGE_URL: body}
        routes.update(net_extra or {})
        path = self.write_sources(**source_overrides)
        return self.fetch(FakeNet(routes), sources_path=path), path

    def test_html_listings_become_records_with_the_documented_id(self):
        (code, emitted, _), path = self.fetch_page(result(PAGE_URL, body=NESTED_LD))
        self.assertEqual(emitted[-1]["status"], "OK")
        code, emitted, _ = self.extract(FakeNet({}), sources_path=path,
                                        extra=["--no-revalidate"])
        self.assertEqual(emitted[-1]["status"], "OK")
        self.assertEqual(emitted[-1]["report_as"], "ok")
        self.assertEqual(emitted[-1]["record_shape"], "full")
        records = self.records()
        self.assertEqual(sorted(r["native_id"] for r in records), ["1001", "1002"])
        first = [r for r in records if r["native_id"] == "1001"][0]
        self.assertEqual(first["title"], "Sunny one bed")
        self.assertEqual(first["price"], "USD 2400")
        self.assertEqual(first["where"], "12 Elm St, Brooklyn")
        self.assertEqual(first["url"], "https://example.test/listing/1001")

    def test_second_run_over_the_same_page_yields_nothing_new(self):
        (_code, _emitted, _), path = self.fetch_page(result(PAGE_URL, body=NESTED_LD))
        self.extract(FakeNet({}), sources_path=path, extra=["--no-revalidate"])
        self.fetch_page(result(PAGE_URL, body=NESTED_LD))
        code, emitted, _ = self.extract(FakeNet({}), sources_path=path,
                                        extra=["--no-revalidate"])
        counts = emitted[-1]["counts"]
        # One row is behind the cursor set by the first run, one is a seen id.
        self.assertEqual(counts["duplicate"] + counts["stale"], 2)
        self.assertEqual(counts["duplicate"], 1)
        self.assertEqual(counts["stale"], 1)
        self.assertEqual(emitted[-1]["records_written"], 0)
        self.assertEqual(emitted[-1]["status"], "OK")
        self.assertEqual(emitted[-1]["report_as"], "nothing_new")
        self.assertEqual(len(self.records()), 2)

    def test_two_rows_sharing_a_native_id_collapse_to_one_record(self):
        page = NESTED_LD.replace('"identifier":"1002"', '"identifier":"1001"') \
                        .replace("/listing/1002", "/listing/1001x")
        (_code, _e, _), path = self.fetch_page(result(PAGE_URL, body=page),
                                               id_rule="jsonld:identifier")
        code, emitted, _ = self.extract(FakeNet({}), sources_path=path,
                                        extra=["--no-revalidate"])
        self.assertEqual(emitted[-1]["counts"]["duplicate"], 1)
        self.assertEqual([r["native_id"] for r in self.records()], ["1001"])

    def test_sitemap_records_are_labelled_discovery_only(self):
        (code, emitted, _), path = self.fetch_page(
            result(PAGE_URL, body=SITEMAP_XML, content_type="application/xml"),
            channel="sitemap", lane="example-test:sitemap")
        self.assertEqual(emitted[-1]["status"], "OK")
        code, emitted, _ = self.extract(FakeNet({}), sources_path=path,
                                        extra=["--no-revalidate"])
        self.assertEqual(emitted[-1]["record_shape"], "discovery-only")
        self.assertEqual(emitted[-1]["records_written"], 2)
        records = self.records()
        self.assertEqual(sorted(r["url"] for r in records),
                         ["https://example.test/listing/2001",
                          "https://example.test/listing/2002"])
        self.assertEqual([r["title"] for r in records], ["", ""])
        self.assertEqual([r["price"] for r in records], ["", ""])

    def test_rss_records_use_the_feed_guid(self):
        (code, emitted, _), path = self.fetch_page(
            result(PAGE_URL, body=RSS_XML, content_type="application/rss+xml"),
            channel="rss", lane="example-test:rss", id_rule="feed:guid")
        self.assertEqual(emitted[-1]["status"], "OK")
        self.extract(FakeNet({}), sources_path=path, extra=["--no-revalidate"])
        self.assertEqual([r["native_id"] for r in self.records()], ["3001"])

    def test_json_api_is_parsed_not_graded_as_an_empty_web_page(self):
        spec = {"record_path": "data.items",
                "fields": {"url": "link", "title": "name", "price": "rent",
                           "where": "hood", "native_id": "id"}}
        (code, emitted, _), path = self.fetch_page(
            result(PAGE_URL, body=API_JSON, content_type="application/json"),
            channel="json", lane="example-test:json", json=spec, id_rule="guid")
        self.assertEqual(emitted[-1]["status"], "OK")     # not EMPTY-GENUINE
        self.assertEqual(emitted[-1]["pages"][0]["listings"], 2)
        code, emitted, _ = self.extract(FakeNet({}), sources_path=path,
                                        extra=["--no-revalidate"])
        records = self.records()
        self.assertEqual(sorted(r["native_id"] for r in records), ["9001", "9002"])
        self.assertEqual([r["price"] for r in records if r["native_id"] == "9001"],
                         ["$2,100"])

    def test_records_from_a_host_outside_the_allowlist_are_refused(self):
        page = NESTED_LD.replace("https://example.test/listing/1002",
                                 "https://elsewhere.test/listing/1002")
        (_code, _e, _), path = self.fetch_page(result(PAGE_URL, body=page))
        code, emitted, _ = self.extract(FakeNet({}), sources_path=path,
                                        extra=["--no-revalidate"])
        self.assertEqual(emitted[-1]["counts"]["urls_refused"], 1)
        self.assertEqual([r["native_id"] for r in self.records()], ["1001"])


# --- revalidation ------------------------------------------------------------


class RevalidationTests(PipelineCase):

    def stage_page(self, **overrides):
        path = self.write_sources(**overrides)
        net = FakeNet({ROBOTS_URL: robots_ok(), PAGE_URL: result(PAGE_URL, body=NESTED_LD)})
        code, _emitted, _ = self.fetch(net, sources_path=path)
        self.assertEqual(code, sources.EXIT_OK)
        return path

    def test_missing_listing_url_pattern_is_a_configuration_error(self):
        path = self.stage_page()
        with open(path) as handle:
            config = json.load(handle)
        config["sources"][0].pop("listing_url_pattern")
        with open(path, "w") as handle:
            json.dump(config, handle)
        code, emitted, message = self.extract(FakeNet({}), sources_path=path)
        self.assertEqual(code, sources.EXIT_SCHEMA)
        self.assertIn("listing_url_pattern", message)
        self.assertEqual(emitted, [])
        self.assertEqual(self.records(), [])

    def test_a_delisted_url_is_dropped_and_a_live_one_is_kept(self):
        path = self.stage_page()
        live = "https://example.test/listing/1001"
        gone = "https://example.test/listing/1002"
        net = FakeNet({live: result(live, status=200, body="ok"),
                       gone: result(gone, status=404, body="")})
        code, emitted, _ = self.extract(net, sources_path=path)
        self.assertEqual(code, sources.EXIT_OK)
        self.assertEqual(emitted[-1]["counts"]["gone"], 1)
        self.assertEqual([r["native_id"] for r in self.records()], ["1001"])
        self.assertTrue(all(call["method"] == "HEAD" for call in net.calls))

    def test_everything_delisted_is_nothing_new_not_a_block(self):
        path = self.stage_page()
        net = FakeNet(dict((url, result(url, status=410, body="")) for url in (
            "https://example.test/listing/1001", "https://example.test/listing/1002")))
        code, emitted, _ = self.extract(net, sources_path=path)
        self.assertEqual(emitted[-1]["counts"]["gone"], 2)
        self.assertEqual(emitted[-1]["status"], "OK")
        self.assertEqual(emitted[-1]["report_as"], "nothing_new")


# --- probe and the verifier --------------------------------------------------


class ProbeTests(PipelineCase):

    def probe(self, net, extra=()):
        return self.run_cli(["probe", "--url", PAGE_URL, "--slug", "example-test",
                             "--install-probe"] + list(extra), net)

    def routes(self, page):
        return {ROBOTS_URL: robots_ok(), "https://example.test/llms.txt":
                result("https://example.test/llms.txt", status=404, body=""),
                PAGE_URL: page}

    def test_html_probe_reports_field_coverage_and_samples(self):
        net = FakeNet(self.routes(result(PAGE_URL, body=NESTED_LD)))
        code, emitted, _ = self.probe(net, extra=["--channel", "html", "--samples", "2"])
        self.assertEqual(code, sources.EXIT_OK)
        record = emitted[-1]
        self.assertEqual(record["status"], "OK")
        self.assertEqual(record["listings"], 2)
        self.assertEqual(record["usable_records"], 2)
        self.assertEqual(record["fields_present"]["price"], 1)
        self.assertEqual(sorted(s["title"] for s in record["samples"]),
                         ["Garden studio", "Sunny one bed"])

    def test_transient_robots_failure_is_not_called_a_retirement(self):
        net = FakeNet({ROBOTS_URL: result(ROBOTS_URL, status=500, body="")})
        code, emitted, _ = self.probe(net)
        self.assertEqual(code, sources.EXIT_OK)
        self.assertEqual(emitted[-1]["status"], "ROBOTS-UNAVAILABLE")
        self.assertEqual(emitted[-1]["report_as"], "source_unchecked")

    def test_json_channel_probe_uses_the_sources_record(self):
        path = self.write_sources(channel="json", lane="example-test:json",
                                  json={"record_path": "data.items",
                                        "fields": {"url": "link", "title": "name"}})
        net = FakeNet(self.routes(result(PAGE_URL, body=API_JSON,
                                         content_type="application/json")))
        code, emitted, _ = self.probe(net, extra=["--sources", path])
        self.assertEqual(code, sources.EXIT_OK)
        self.assertEqual(emitted[-1]["channel"], "json")
        self.assertEqual(emitted[-1]["status"], "OK")
        self.assertEqual(emitted[-1]["listings"], 2)
        self.assertEqual(emitted[-1]["usable_records"], 2)


class VerifierTests(SimpleTestCase):

    def test_useful_requires_a_title_and_a_url_not_just_a_node_count(self):
        self.assertEqual(verify.verdict({"status": "OK", "listings": 5,
                                         "usable_records": 0}), (True, True, False))
        self.assertEqual(verify.verdict({"status": "OK", "listings": 5,
                                         "usable_records": 3}), (True, True, True))

    def test_empty_genuine_is_parseable_but_not_useful(self):
        reachable, parseable, useful = verify.verdict(
            {"status": "EMPTY-GENUINE", "listings": 0, "usable_records": 0})
        self.assertEqual((reachable, parseable, useful), (True, True, False))

    def test_transient_robots_status_is_not_reachable(self):
        for status in ("ROBOTS-UNAVAILABLE", "ROBOTS-DISALLOWED", "BLOCKED-EDGE"):
            with self.subTest(status=status):
                self.assertEqual(verify.verdict({"status": status})[0], False)

    def test_probe_is_pinned_to_the_html_lane(self):
        seen = {}

        def fake_run(cmd, timeout=120):
            seen["cmd"] = cmd
            return {"status": "OK", "listings": 1, "usable_records": 1}, ""

        with mock.patch.object(verify, "run", fake_run):
            verify.probe("https://example.test/rent", "browser", "residential", samples=2)
        self.assertIn("--channel", seen["cmd"])
        self.assertEqual(seen["cmd"][seen["cmd"].index("--channel") + 1], "html")
        self.assertEqual(seen["cmd"][seen["cmd"].index("--samples") + 1], "2")

    def test_summary_line_reports_usable_and_priced_counts(self):
        line, count = verify.summarize({"status": "OK", "listings": 4,
                                        "usable_records": 3,
                                        "fields_present": {"price": 2}})
        self.assertEqual(count, 4)
        self.assertIn("3 usable (2 priced)", line)
