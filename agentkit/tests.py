"""Route, header and integrity tests for the public agent kit.

``SimpleTestCase`` rather than ``TestCase`` on purpose: these routes must never touch the
database or a session, and ``SimpleTestCase`` turns that from a claim into an assertion.
"""

import contextlib
import email.message
import hashlib
import importlib.util
import io
import json
import logging
import os
import sys
import pathlib
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import zipfile
from datetime import timedelta
from unittest import mock

from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from accounts.models import AgentLink, User, device_code_digest
from agentkit import packaging

ORIGIN = "https://homing.example"
MARKDOWN = "text/markdown; charset=utf-8"
PLAIN = "text/plain; charset=utf-8"


def url_for(relpath):
    """Public URL of a manifest path.  Mirrors the routes in ``agentkit/urls.py``."""
    return f"/agent/pkg/{relpath}"


class AgentKitCase(SimpleTestCase):
    """Shared setup: the package the views will serve for this test's origin."""

    def setUp(self):
        self.client = Client()
        self.package = packaging.build_package(ORIGIN)

    def fixed_routes(self):
        return [
            "/agent/",
            "/agent/pkg/VERSION",
            "/agent/pkg/manifest.json",
            "/agent/pkg/SKILL.md",
            f"/agent/pkg/{self.package.archive_name}",
        ]

    def all_routes(self):
        return self.fixed_routes() + [url_for(p) for p in self.package.manifest_paths]


@override_settings(PUBLIC_BASE_URL=ORIGIN)
class AgentKitPublicTests(AgentKitCase):
    """Every route must answer an agent that has no session and never will."""

    def test_every_route_is_public(self):
        for route in self.all_routes():
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 200)

    def test_no_download_or_cookie_headers(self):
        for route in self.all_routes():
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertNotIn("Content-Disposition", response)
                self.assertNotIn("Cookie", response.get("Vary", ""))
                self.assertIn("public", response["Cache-Control"])
                self.assertNotIn("no-store", response["Cache-Control"])
                self.assertNotIn("private", response["Cache-Control"])

    def test_content_types(self):
        cases = {
            "/agent/": MARKDOWN,
            "/agent/pkg/SKILL.md": MARKDOWN,
            "/agent/pkg/VERSION": PLAIN,
            "/agent/pkg/manifest.json": "application/json",
            f"/agent/pkg/{self.package.archive_name}": "application/zip",
        }
        for route, content_type in cases.items():
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route)["Content-Type"], content_type)

    def test_reference_and_script_content_types(self):
        for relpath in self.package.manifest_paths:
            if not relpath.startswith(("references/", "scripts/")):
                continue
            expected = MARKDOWN if relpath.endswith(".md") else PLAIN
            with self.subTest(relpath=relpath):
                served = self.client.get(url_for(relpath))
                self.assertEqual(served["Content-Type"], expected)

    def test_cache_lifetimes(self):
        self.assertEqual(self.client.get("/agent/")["Cache-Control"], "public, max-age=300")
        self.assertEqual(
            self.client.get("/agent/pkg/VERSION")["Cache-Control"], "public, max-age=3600"
        )

    def test_version_is_an_integer(self):
        body = self.client.get("/agent/pkg/VERSION").content.decode("utf-8")
        self.assertEqual(body, f"{self.package.version}\n")
        self.assertGreaterEqual(int(body.strip()), 1)


@override_settings(PUBLIC_BASE_URL=ORIGIN)
class AgentKitManifestTests(AgentKitCase):
    def manifest(self):
        return json.loads(self.client.get("/agent/pkg/manifest.json").content)

    def test_manifest_shape(self):
        manifest = self.manifest()
        self.assertEqual(manifest["package"], "homing-agent-kit")
        self.assertEqual(manifest["version"], self.package.version)
        self.assertEqual(manifest["generated_for_origin"], ORIGIN)
        self.assertEqual(manifest["archive"]["path"], self.package.archive_name)
        self.assertTrue(manifest["files"])
        self.assertEqual(
            sorted(manifest["files"], key=lambda e: e["path"]), manifest["files"]
        )

    def test_manifest_digests_match_served_bytes(self):
        for entry in self.manifest()["files"]:
            with self.subTest(path=entry["path"]):
                served = self.client.get(url_for(entry["path"]))
                self.assertEqual(served.status_code, 200)
                self.assertEqual(hashlib.sha256(served.content).hexdigest(), entry["sha256"])
                self.assertEqual(len(served.content), entry["bytes"])
                self.assertEqual(len(served.content.splitlines()), entry["lines"])

    def test_archive_matches_manifest_exactly(self):
        manifest = self.manifest()
        body = self.client.get(f"/agent/pkg/{self.package.archive_name}").content
        self.assertEqual(hashlib.sha256(body).hexdigest(), manifest["archive"]["sha256"])
        self.assertEqual(len(body), manifest["archive"]["bytes"])
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertEqual(
                sorted(archive.namelist()), sorted(e["path"] for e in manifest["files"])
            )
            for entry in manifest["files"]:
                member = archive.read(entry["path"])
                self.assertEqual(hashlib.sha256(member).hexdigest(), entry["sha256"])

    def test_archive_bytes_are_deterministic(self):
        first = packaging.build_fresh(ORIGIN)
        second = packaging.build_fresh(ORIGIN)
        self.assertEqual(first.archive_bytes, second.archive_bytes)
        self.assertEqual(first.manifest, second.manifest)

    def test_index_is_not_in_the_archive(self):
        # index.md is fetched before the manifest exists to the agent, and it is the one
        # package file with no /agent/pkg/ URL.  Keeping it out preserves the rule that every
        # manifest path is fetchable.
        self.assertNotIn("index.md", self.package.manifest_paths)

    def test_every_manifest_path_is_routable(self):
        for relpath in self.package.manifest_paths:
            with self.subTest(relpath=relpath):
                self.assertTrue(packaging.is_routable(relpath))

    def test_unknown_archive_version_is_404(self):
        other = self.package.version + 1
        response = self.client.get(f"/agent/pkg/homing-agent-kit-{other}.zip")
        self.assertEqual(response.status_code, 404)


@override_settings(PUBLIC_BASE_URL=ORIGIN)
class AgentKitCachingTests(AgentKitCase):
    def test_etag_round_trip_returns_304(self):
        for route in self.fixed_routes():
            with self.subTest(route=route):
                first = self.client.get(route)
                etag = first["ETag"]
                self.assertEqual(etag, f'"{hashlib.sha256(first.content).hexdigest()}"')
                second = self.client.get(route, headers={"if-none-match": etag})
                self.assertEqual(second.status_code, 304)
                self.assertEqual(second["ETag"], etag)
                self.assertEqual(second.content, b"")

    def test_stale_etag_returns_the_body(self):
        response = self.client.get("/agent/", headers={"if-none-match": '"stale"'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content)

    def test_wildcard_weak_and_listed_etags_match(self):
        etag = self.client.get("/agent/pkg/VERSION")["ETag"]
        for header in ("*", f"W/{etag}", f'"other", {etag}'):
            with self.subTest(header=header):
                response = self.client.get("/agent/pkg/VERSION", headers={"if-none-match": header})
                self.assertEqual(response.status_code, 304)


class AgentKitTraversalTests(SimpleTestCase):
    def test_traversal_and_unknown_names_404(self):
        client = Client()
        attempts = [
            "/agent/pkg/references/../../../etc/passwd.md",
            "/agent/pkg/references/..%2F..%2Fetc%2Fpasswd.md",
            "/agent/pkg/references/%2e%2e%2fSKILL.md",
            "/agent/pkg/references/...md",
            "/agent/pkg/scripts/../SKILL.md",
            "/agent/pkg/scripts/..%2FSKILL.md",
            "/agent/pkg/scripts//etc/passwd",
            "/agent/pkg/scripts/%2Fetc%2Fpasswd",
            "/agent/pkg/scripts/%2Fetc%2Fshadow",
            "/agent/pkg/references/nope.md",
            "/agent/pkg/scripts/nope.sh",
            "/agent/pkg/VERSION.md",
            "/agent/pkg/index.md",
        ]
        for attempt in attempts:
            with self.subTest(attempt=attempt):
                self.assertEqual(client.get(attempt).status_code, 404)

    def test_unsafe_methods_rejected(self):
        client = Client()
        self.assertEqual(client.post("/agent/").status_code, 405)
        self.assertEqual(client.post("/agent/pkg/SKILL.md").status_code, 405)


@override_settings(PUBLIC_BASE_URL=ORIGIN)
class AgentKitOriginTests(AgentKitCase):
    def package_bodies(self):
        """Served bytes of every package file -- the manifest is excluded on purpose."""
        routes = ["/agent/"] + [url_for(p) for p in self.package.manifest_paths]
        return {route: self.client.get(route).content for route in routes}

    def test_placeholder_never_escapes(self):
        bodies = dict(self.package_bodies())
        bodies["/agent/pkg/manifest.json"] = self.client.get("/agent/pkg/manifest.json").content
        for route, body in bodies.items():
            with self.subTest(route=route):
                self.assertNotIn(b"__HOMING_ORIGIN__", body)

    def test_real_origin_is_substituted_in(self):
        joined = b"".join(self.package_bodies().values())
        self.assertIn(ORIGIN.encode("utf-8"), joined)

    def test_archive_carries_the_substituted_origin(self):
        body = self.client.get(f"/agent/pkg/{self.package.archive_name}").content
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            members = b"".join(archive.read(name) for name in archive.namelist())
        self.assertNotIn(b"__HOMING_ORIGIN__", members)
        self.assertIn(ORIGIN.encode("utf-8"), members)


class AgentKitOriginResolutionTests(SimpleTestCase):
    def origin_served(self):
        return json.loads(Client().get("/agent/pkg/manifest.json").content)[
            "generated_for_origin"
        ]

    @override_settings(PUBLIC_BASE_URL="https://configured.example/")
    def test_public_base_url_wins_and_loses_its_slash(self):
        self.assertEqual(self.origin_served(), "https://configured.example")

    @override_settings(PUBLIC_BASE_URL="")
    def test_falls_back_to_the_request(self):
        self.assertEqual(self.origin_served(), "http://testserver")


# ---------------------------------------------------------------------------
# Device-code pairing.  These drive the installed CLI end to end against the
# real /api/v1/agent-link* views: homing.py's own urllib opener is replaced by a
# shim that hands each request to Django's test client, so everything above the
# socket -- URL assembly, the error table, the exit codes, the store write and
# the verifying read -- is the shipped code.  No live host is ever contacted.
# ---------------------------------------------------------------------------

HOMING_PY = pathlib.Path(__file__).resolve().parent / "package" / "scripts" / "homing.py"
SELFTEST_PY = pathlib.Path(__file__).resolve().parent / "package" / "scripts" / "selftest.py"
PAIR_ORIGIN = "https://homing.example"


def load_homing_cli():
    """A fresh module per test: the redaction filter and key cache are process state."""
    spec = importlib.util.spec_from_file_location("homing_cli_under_test", HOMING_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ORIGIN = PAIR_ORIGIN  # the installer's literal substitution, done here
    return module


class _Raw:
    """Minimal stand-in for what urlopen returns."""

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self._body = body

    def read(self, amount=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class DjangoTransport:
    """homing.py's opener, rerouted into the Django test client.

    ``rewrite`` lets a test corrupt a response without touching the server; it is
    the only way to reach the malformed-response branch.
    """

    def __init__(self, client):
        self.client = client
        self.calls = []
        self.rewrite = None

    def open(self, request, timeout=None):
        parts = urllib.parse.urlsplit(request.full_url)
        path = parts.path + (("?" + parts.query) if parts.query else "")
        headers = {k.lower(): v for k, v in request.header_items()}
        headers.pop("content-type", None)
        body = request.data or b""
        self.calls.append({
            "method": request.get_method(),
            "path": path,
            "headers": headers,
            "body": body.decode("utf-8", "replace"),
        })
        response = self.client.generic(
            request.get_method(), path, body,
            content_type="application/json", headers=headers,
        )
        status, content = response.status_code, response.content
        pairs = list(response.items())
        if self.rewrite:
            status, pairs, content = self.rewrite(status, pairs, content)
        message = email.message.Message()
        for name, value in pairs:
            message[name] = value
        if status >= 300:
            raise urllib.error.HTTPError(
                request.full_url, status, "", message, io.BytesIO(content)
            )
        return _Raw(status, message, content)


class PairingCLICase(TestCase):
    """Shared rig: someone who can approve, a temp tree, and a captured CLI."""

    def setUp(self):
        self.user = User.objects.create_user(
            "pair-cli@example.com", password="correct horse battery staple"
        )
        self.homing = load_homing_cli()
        self.transport = DjangoTransport(Client())
        self.homing._OPENER = self.transport

        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.private = os.path.join(self.tmp, "private")  # helper-only, never read by a model
        self.state = os.path.join(self.tmp, "state")      # agent-readable
        os.makedirs(self.private, mode=0o700)
        os.makedirs(self.state, mode=0o700)
        self.device_code_file = os.path.join(self.private, "device-code")
        self.out_file = os.path.join(self.state, "pair-request.json")
        self.result_file = os.path.join(self.state, "pair-result.json")
        self.store_file = os.path.join(self.private, "token")

        # Never the real login keychain: the file store is the one with no GUI.
        patcher = mock.patch.dict(os.environ, {
            "HOMING_TOKEN_STORE": "file",
            "HOMING_TOKEN_FILE": self.store_file,
        })
        patcher.start()
        self.addCleanup(patcher.stop)

        self.slept = []
        self.stdout = ""
        self.stderr = ""
        self.last_argv = []

    # -- driving the CLI ---------------------------------------------------

    def run_cli(self, argv):
        """Run homing.py in-process, capturing everything it can possibly emit."""
        self.homing.LOG.handlers[:] = []
        del self.homing._TOKEN_CACHE[:]
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.homing.main(argv) or 0
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 0
        self.stdout, self.stderr = out.getvalue(), err.getvalue()
        self.last_argv = list(argv)
        return code

    @contextlib.contextmanager
    def sleeper(self, on_sleep=None):
        """Run the protocol on a fake clock: waits are recorded, not endured.

        ``time.time`` moves only when the client sleeps, so the give-up deadline
        is measured against the delays the client actually chose.
        """
        clock = [time.time()]

        def fake_sleep(seconds):
            self.slept.append(seconds)
            clock[0] += seconds
            if on_sleep:
                on_sleep(len(self.slept))

        with mock.patch.object(time, "sleep", fake_sleep), \
                mock.patch.object(time, "time", lambda: clock[0]):
            yield

    def unblock(self):
        """Pretend the interval elapsed, so the next poll is not a slow_down."""
        AgentLink.objects.all().update(last_polled_at=None)

    def approve_on_first_sleep(self, index):
        self.unblock()
        if index == 1:
            self.link().approve(self.user)

    # -- steps -------------------------------------------------------------

    def pair_request(self, extra=()):
        argv = ["pair-request", "--label", "Claude on Hart's MacBook",
                "--note", "macOS laptop, runs while logged in", "--cadence", "180",
                "--out", self.out_file, "--device-code-out", self.device_code_file]
        code = self.run_cli(argv + list(extra))
        self.assertEqual(code, 0, self.stderr)
        return code

    def poll_argv(self, store=True, timeout=600, interval=5):
        argv = ["pair-poll", "--device-code-file", self.device_code_file,
                "--result", self.result_file, "--timeout", str(timeout),
                "--interval", str(interval)]
        if store:
            argv.append("--store")
        return argv

    # -- reading the artefacts ---------------------------------------------

    def read_json(self, path):
        with open(path) as handle:
            return json.load(handle)

    def read_text(self, path):
        with open(path) as handle:
            return handle.read()

    def device_code(self):
        return self.read_text(self.device_code_file).strip()

    def link(self):
        return AgentLink.objects.get()

    def mode_of(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)


@override_settings(PUBLIC_BASE_URL=PAIR_ORIGIN)
class PairRequestTests(PairingCLICase):
    def test_request_creates_a_pending_link_and_exposes_only_safe_metadata(self):
        self.pair_request()
        link = self.link()
        self.assertEqual(link.status, AgentLink.Status.PENDING)
        self.assertEqual(link.agent_label, "Claude on Hart's MacBook")
        self.assertEqual(link.environment_note, "macOS laptop, runs while logged in")
        self.assertEqual(link.requested_cadence_minutes, 180)

        written = self.read_json(self.out_file)
        self.assertEqual(
            set(written),
            {"user_code", "verification_uri", "verification_uri_complete",
             "expires_at", "interval"},
        )
        self.assertEqual(written["user_code"], link.user_code)
        self.assertEqual(written["interval"], link.interval_seconds)
        self.assertEqual(written["verification_uri"], f"{PAIR_ORIGIN}/link/")
        self.assertEqual(
            written["verification_uri_complete"],
            f"{PAIR_ORIGIN}/link/?code={link.user_code}",
        )
        self.assertTrue(written["expires_at"].endswith("Z"))
        self.assertEqual(json.loads(self.stdout), dict(written, ok=True))

    def test_the_device_code_is_real_private_and_never_shown(self):
        self.pair_request()
        device_code = self.device_code()
        # The file holds the code the server actually minted, and nothing else.
        self.assertEqual(self.link().device_code_hash, device_code_digest(device_code))
        self.assertEqual(self.read_text(self.device_code_file), device_code)

        self.assertNotIn(device_code, self.stdout)
        self.assertNotIn(device_code, self.stderr)
        self.assertNotIn(device_code, self.read_text(self.out_file))
        self.assertFalse(any(device_code in arg for arg in self.last_argv))

    def test_both_files_are_owner_only(self):
        self.pair_request()
        self.assertEqual(self.mode_of(self.device_code_file), 0o600)
        self.assertEqual(self.mode_of(self.out_file), 0o600)

    def test_an_unusable_label_is_refused_before_any_request(self):
        code = self.run_cli(["pair-request", "--label", "   ", "--out", self.out_file,
                             "--device-code-out", self.device_code_file])
        self.assertEqual(code, self.homing.EXIT_USAGE)
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(os.path.exists(self.device_code_file))

    def test_help_needs_no_network_and_no_key(self):
        for argv in (["--help"], ["pair-request", "--help"], ["pair-poll", "--help"]):
            with self.subTest(argv=argv):
                self.homing._OPENER = None  # any request at all would raise
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        self.homing.main(argv)
                self.assertEqual(caught.exception.code, 0)
                self.homing._OPENER = self.transport


@override_settings(PUBLIC_BASE_URL=PAIR_ORIGIN)
class PairPollTests(PairingCLICase):
    def test_pending_then_approved_stores_and_verifies_the_key(self):
        self.pair_request()
        with self.sleeper(self.approve_on_first_sleep):
            code = self.run_cli(self.poll_argv())
        self.assertEqual(code, 0, self.stderr)

        printed = json.loads(self.stdout)
        self.assertTrue(printed["paired"])
        self.assertTrue(printed["stored"])
        self.assertTrue(printed["verified"])
        self.assertIsNone(printed["error_class"])

        result = self.read_json(self.result_file)
        self.assertEqual(set(result), {"paired", "error_class", "expires_at", "scopes"})
        self.assertTrue(result["paired"])
        self.assertIsNone(result["error_class"])
        self.assertTrue(result["expires_at"])
        self.assertIn("leads:write", result["scopes"])
        self.assertNotIn("leads:destroy", result["scopes"])

        link = self.link()
        self.assertEqual(link.status, AgentLink.Status.CONSUMED)
        self.assertIsNotNone(link.issued_token_id)

        # The key really is in the store, and it really is the issued one: the
        # CLI's own verifying read went out carrying it and came back 200.
        stored = self.read_text(self.store_file).strip()
        self.assertTrue(stored)
        verify = [c for c in self.transport.calls if c["path"] == "/api/v1/me/token"]
        self.assertEqual(len(verify), 1)
        self.assertEqual(verify[0]["headers"]["authorization"], "Bearer " + stored)
        self.assertFalse(os.path.exists(self.device_code_file))

    def test_pairing_without_store_never_writes_the_key_anywhere(self):
        self.pair_request()
        with self.sleeper(self.approve_on_first_sleep):
            code = self.run_cli(self.poll_argv(store=False))
        self.assertEqual(code, 0, self.stderr)
        self.assertEqual(json.loads(self.stdout)["error_class"], "not_stored")
        self.assertFalse(os.path.exists(self.store_file))
        self.assertEqual(self.read_json(self.result_file)["error_class"], "not_stored")

    def test_polling_too_fast_is_told_to_slow_down_and_backs_off(self):
        self.pair_request()

        def approve_after_two(index):
            if index >= 2:
                self.unblock()
            if index == 2:
                self.link().approve(self.user)

        with self.sleeper(approve_after_two):
            code = self.run_cli(self.poll_argv())
        self.assertEqual(code, 0, self.stderr)
        # pending (wait 5), slow_down (interval +5, wait 10), then approved.
        self.assertEqual(self.slept, [5, 10])

    def test_a_denied_pairing_stops_immediately(self):
        self.pair_request()
        self.link().deny(self.user)
        with self.sleeper():
            code = self.run_cli(self.poll_argv())
        self.assertEqual(code, self.homing.EXIT_AUTH)
        self.assertEqual(self.slept, [])  # not one retry
        result = self.read_json(self.result_file)
        self.assertFalse(result["paired"])
        self.assertEqual(result["error_class"], "access_denied")
        self.assertFalse(os.path.exists(self.device_code_file))
        self.assertFalse(os.path.exists(self.store_file))

    def test_an_expired_pairing_asks_for_a_new_code(self):
        self.pair_request()
        AgentLink.objects.all().update(expires_at=timezone.now() - timedelta(seconds=1))
        with self.sleeper():
            code = self.run_cli(self.poll_argv())
        self.assertEqual(code, self.homing.EXIT_TEMPFAIL)
        self.assertEqual(self.read_json(self.result_file)["error_class"], "expired_token")
        self.assertFalse(os.path.exists(self.device_code_file))
        self.assertFalse(os.path.exists(self.store_file))

    def test_an_unknown_device_code_is_treated_as_a_denial(self):
        self.pair_request()
        self.homing._write_private_text(self.device_code_file, "not-a-real-device-code")
        with self.sleeper():
            code = self.run_cli(self.poll_argv())
        self.assertEqual(code, self.homing.EXIT_AUTH)
        self.assertEqual(self.read_json(self.result_file)["error_class"], "access_denied")
        self.assertFalse(os.path.exists(self.device_code_file))

    def test_giving_up_at_the_timeout_leaves_nothing_behind(self):
        self.pair_request()
        with self.sleeper(lambda index: self.unblock()):
            code = self.run_cli(self.poll_argv(timeout=12, interval=5))
        self.assertEqual(code, self.homing.EXIT_TEMPFAIL)
        self.assertEqual(self.slept, [5, 5])  # a third wait would land past 12s
        self.assertEqual(self.read_json(self.result_file)["error_class"], "timeout")
        self.assertFalse(os.path.exists(self.device_code_file))
        self.assertFalse(os.path.exists(self.store_file))

    def test_a_malformed_success_is_not_treated_as_a_key(self):
        self.pair_request()
        self.link().approve(self.user)

        def strip_the_token(status, pairs, content):
            if status == 200 and b"token" in content:
                return 200, pairs, b'{"expires_at": "2099-01-01T00:00:00Z"}'
            return status, pairs, content

        self.transport.rewrite = strip_the_token
        with self.sleeper():
            code = self.run_cli(self.poll_argv())
        self.assertEqual(code, self.homing.EXIT_UNAVAILABLE)
        self.assertEqual(self.read_json(self.result_file)["error_class"], "malformed_response")
        self.assertFalse(os.path.exists(self.store_file))
        self.assertFalse(os.path.exists(self.device_code_file))

    def test_a_device_code_buys_exactly_one_key(self):
        self.pair_request()
        device_code = self.device_code()
        with self.sleeper(self.approve_on_first_sleep):
            self.assertEqual(self.run_cli(self.poll_argv()), 0, self.stderr)
        first_key = self.read_text(self.store_file)

        self.homing._write_private_text(self.device_code_file, device_code)
        self.unblock()
        with self.sleeper():
            code = self.run_cli(self.poll_argv())
        self.assertEqual(code, self.homing.EXIT_AUTH)
        self.assertEqual(self.read_json(self.result_file)["error_class"], "access_denied")
        self.assertEqual(self.read_text(self.store_file), first_key)  # nothing new arrived

    def test_a_world_readable_device_code_is_never_spent(self):
        self.pair_request()
        os.chmod(self.device_code_file, 0o644)
        with self.sleeper():
            code = self.run_cli(self.poll_argv())
        self.assertEqual(code, self.homing.EXIT_CONFIG)
        self.assertEqual(self.read_json(self.result_file)["error_class"], "no_device_code")
        self.assertEqual(len(self.transport.calls), 1)  # only pair-request's own call
        self.assertFalse(os.path.exists(self.device_code_file))
        self.assertEqual(self.link().status, AgentLink.Status.PENDING)


@override_settings(PUBLIC_BASE_URL=PAIR_ORIGIN)
class PairingSecretHygieneTests(PairingCLICase):
    """Neither credential may occur in anything a person or a model can read."""

    def surfaces(self, transcript):
        """Every place output can land, minus the store, where a key belongs."""
        agent_link = [c for c in self.transport.calls
                      if c["path"].startswith("/api/v1/agent-link")]
        return {
            "stdout": transcript["stdout"],
            "stderr": transcript["stderr"],
            "argv": json.dumps(transcript["argv"]),
            "environment": json.dumps(dict(os.environ)),
            "logs": transcript["logs"],
            "pair-request.json": self.read_text(self.out_file),
            "pair-result.json": self.read_text(self.result_file),
            # The device code belongs in the pairing request body and nowhere
            # else, so only the headers of those calls are scanned.
            "outbound headers": json.dumps([c["headers"] for c in agent_link]),
        }

    def test_neither_the_device_code_nor_the_key_escapes(self):
        transcript = {"stdout": "", "stderr": "", "argv": [], "logs": ""}
        captured = io.StringIO()

        self.pair_request()
        transcript["stdout"] += self.stdout
        transcript["stderr"] += self.stderr
        transcript["argv"] += self.last_argv
        device_code = self.device_code()

        # --verbose is the loudest this client gets; everything it says is
        # captured here through the shipped redaction filter.
        with self.sleeper(self.approve_on_first_sleep):
            code = self.run_cli(["--verbose"] + self.poll_argv())
        self.assertEqual(code, 0, self.stderr)
        transcript["stdout"] += self.stdout
        transcript["stderr"] += self.stderr
        transcript["argv"] += self.last_argv

        handler = logging.StreamHandler(captured)
        handler.addFilter(self.homing.REDACTOR)
        self.homing.LOG.handlers[:] = [handler]
        self.homing.LOG.error("device=%s key=%s", device_code,
                              self.read_text(self.store_file).strip())
        transcript["logs"] = captured.getvalue()
        self.assertIn("<redacted>", transcript["logs"])  # the filter did fire

        token = self.read_text(self.store_file).strip()
        self.assertTrue(token)
        self.assertNotEqual(token, device_code)
        for name, haystack in self.surfaces(transcript).items():
            with self.subTest(surface=name):
                self.assertNotIn(device_code, haystack)
                self.assertNotIn(token, haystack)

    def test_the_key_reaches_the_store_and_the_authorization_header_only(self):
        self.pair_request()
        with self.sleeper(self.approve_on_first_sleep):
            self.assertEqual(self.run_cli(self.poll_argv()), 0, self.stderr)
        token = self.read_text(self.store_file).strip()

        for call in self.transport.calls:
            with self.subTest(path=call["path"]):
                self.assertNotIn(token, call["body"])
                self.assertNotIn(token, call["path"])
                if call["path"].startswith("/api/v1/agent-link"):
                    self.assertNotIn("authorization", call["headers"])


@override_settings(PUBLIC_BASE_URL=PAIR_ORIGIN)
class PairingPrivateStateTests(PairingCLICase):
    """The device-code file: owner-only while it exists, gone once it is spent."""

    def test_every_written_file_is_owner_only(self):
        self.pair_request()
        self.assertEqual(self.mode_of(self.device_code_file), 0o600)
        with self.sleeper(self.approve_on_first_sleep):
            self.assertEqual(self.run_cli(self.poll_argv()), 0, self.stderr)
        self.assertEqual(self.mode_of(self.result_file), 0o600)
        self.assertEqual(self.mode_of(self.store_file), 0o600)

    def test_the_device_code_file_is_gone_after_every_ending(self):
        def deny(index):
            self.link().deny(self.user)

        def expire(index):
            AgentLink.objects.all().update(expires_at=timezone.now() - timedelta(seconds=1))

        for ending, before in (("success", None), ("denial", deny), ("expiry", expire)):
            with self.subTest(ending=ending):
                AgentLink.objects.all().delete()
                self.pair_request()
                if before:
                    before(0)
                with self.sleeper(self.approve_on_first_sleep if not before else None):
                    self.run_cli(self.poll_argv(store=(ending == "success")))
                self.assertFalse(os.path.exists(self.device_code_file))

    def test_an_interrupted_poll_still_shreds_the_device_code(self):
        self.pair_request()

        def interrupt(index):
            raise KeyboardInterrupt

        with self.sleeper(interrupt):
            code = self.run_cli(self.poll_argv())
        self.assertEqual(code, self.homing.EXIT_TEMPFAIL)
        self.assertFalse(os.path.exists(self.device_code_file))
        self.assertFalse(os.path.exists(self.store_file))
        result = self.read_json(self.result_file)
        self.assertFalse(result["paired"])
        self.assertEqual(result["error_class"], "interrupted")
        self.assertEqual(self.mode_of(self.result_file), 0o600)
        self.assertEqual(self.link().status, AgentLink.Status.PENDING)


class KeychainWriteIsProvenNotAssumed(SimpleTestCase):
    """Two real failures on real hardware, one after the other.

    First `security add-generic-password -w` with no value sat on a /dev/tty
    prompt a pipe could never answer, and timed out. Then `security -i` exited 0
    while the item did not become findable, and pairing reported stored: true
    for a key nothing could read. A write that cannot be demonstrated is not a
    write, so every mechanism now reads the value back before claiming success.
    """

    def setUp(self):
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "package", "scripts"))
        sys.modules.pop("homing", None)
        import homing
        self.homing = homing
        self.addCleanup(lambda: sys.path.pop(0))

    def test_success_requires_the_value_to_read_back(self):
        self.homing._feed_quiet = lambda *a, **k: 0
        self.homing._keychain_read = lambda s, a: "the-key"
        self.assertEqual(self.homing._store_in_keychain("the-key"), 0)

    def test_keychain_read_puts_the_account_after_the_service_value(self):
        calls = []
        self.homing._run_quiet = lambda argv: (calls.append(argv) or (b"the-key\n", 0))
        self.assertEqual(self.homing._keychain_read("homing-api-token", "alice"), "the-key")
        self.assertEqual(calls, [[
            "/usr/bin/security", "find-generic-password",
            "-s", "homing-api-token", "-a", "alice", "-w",
        ]])

    def test_runtime_keychain_read_uses_the_same_valid_argument_order(self):
        calls = []
        self.homing._run_quiet = lambda argv: (calls.append(argv) or (b"the-key\n", 0))
        with mock.patch.dict(os.environ, {
            "HOMING_KEYCHAIN_SERVICE": "homing-api-token",
            "HOMING_KEYCHAIN_ACCOUNT": "alice",
        }):
            self.assertEqual(self.homing._token_from_keychain(), "the-key")
        self.assertEqual(calls, [[
            "/usr/bin/security", "find-generic-password",
            "-s", "homing-api-token", "-a", "alice", "-w",
        ]])

    def test_selftest_keychain_read_uses_the_same_valid_argument_order(self):
        spec = importlib.util.spec_from_file_location("selftest_under_test", SELFTEST_PY)
        selftest = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(selftest)
        calls = []
        selftest.run_quiet = lambda argv, **kw: (calls.append(argv) or (b"the-key\n", 0))
        manifest = {"secret_store": {
            "kind": "keychain", "service": "homing-api-token", "account": "alice",
        }}
        token, error = selftest.read_stored_token(manifest, True)
        self.assertEqual((token, error), ("the-key", ""))
        self.assertEqual(calls, [[
            "/usr/bin/security", "find-generic-password",
            "-s", "homing-api-token", "-a", "alice", "-w",
        ]])

    def test_exit_zero_is_not_enough(self):
        """The exact second failure: the helper succeeds, the item is absent."""
        self.homing._feed_quiet = lambda *a, **k: 0
        self.homing._keychain_read = lambda s, a: None
        self.assertEqual(self.homing._store_in_keychain("the-key"),
                         self.homing.EXIT_STORE_UNVERIFIED)

    def test_a_different_value_reading_back_is_a_failure(self):
        self.homing._feed_quiet = lambda *a, **k: 0
        self.homing._keychain_read = lambda s, a: "some-other-key"
        self.assertEqual(self.homing._store_in_keychain("the-key"),
                         self.homing.EXIT_STORE_UNVERIFIED)

    def test_the_proven_mechanism_runs_first(self):
        """`security -i` is the one measured to write and read back."""
        calls = []
        self.homing._feed_quiet = lambda argv, text, **kw: calls.append(argv) or 0
        self.homing._keychain_read = lambda s, a: "k"
        self.homing._store_in_keychain("k")
        self.assertEqual(calls[0], ["/usr/bin/security", "-i"])

    def test_prompt_mode_is_only_tried_detached(self):
        """With a terminal it exits 0 and stores the wrong value."""
        calls = []
        self.homing._feed_quiet = lambda argv, text, **kw: (
            calls.append((argv, kw.get("new_session"))) or 0)
        self.homing._keychain_read = lambda s, a: None
        self.homing._store_in_keychain("k")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1][1], "prompt mode must run detached from any tty")

    def test_nothing_passed_to_security_i_contains_a_space(self):
        """`-i` neither strips quotes nor joins quoted words.

        A label of "Homing API token" parsed as three arguments and exited 2;
        quoting the service stored the quotes as part of the name.
        """
        texts = []
        self.homing._feed_quiet = lambda argv, text, **kw: texts.append(text) or 0
        self.homing._keychain_read = lambda s, a: None
        self.homing._store_in_keychain("k")
        stream = [t for t in texts if "add-generic-password" in t][0]
        self.assertNotIn('"', stream)
        self.assertNotIn("'", stream)
        # every token after the flags is a single bare word
        self.assertNotIn(" API ", stream)
        self.assertIn("Homing-API-token", stream)

    def test_the_key_never_reaches_argv(self):
        seen = []
        self.homing._feed_quiet = lambda argv, text, **kw: seen.append(argv) or 0
        self.homing._keychain_read = lambda s, a: None
        self.homing._store_in_keychain("super-secret-value")
        for argv in seen:
            self.assertNotIn("super-secret-value", " ".join(argv))

    def test_a_key_that_would_break_the_tokenizer_is_refused(self):
        self.homing._feed_quiet = lambda *a, **k: 0
        self.homing._keychain_read = lambda s, a: None
        self.assertEqual(self.homing._store_in_keychain("has space"),
                         self.homing.EXIT_STORE_UNQUOTABLE)
        self.assertEqual(self.homing._store_in_keychain('has"quote'),
                         self.homing.EXIT_STORE_UNQUOTABLE)

    def test_timeout_sentinel_is_not_a_plausible_exit_code(self):
        self.assertEqual(self.homing.EXIT_STORE_PROMPTED, 79)
        self.assertNotEqual(self.homing.EXIT_STORE_PROMPTED, 62)

    def test_the_helpers_own_error_is_kept_and_redacted(self):
        """Three real failures reported only an exit number.

        `security` explained itself every time and the explanation went to
        /dev/null, so each round of debugging was a guess. It is captured now,
        and scrubbed, because the text can quote the key back at us.
        """
        self.homing.REDACTOR.add("SECRET-KEY-VALUE")
        code = self.homing._feed_quiet(
            ["/bin/sh", "-c", "echo 'refused: SECRET-KEY-VALUE bad'; exit 3"], "x")
        self.assertEqual(code, 3)
        self.assertIn("refused", self.homing.STORE_DIAGNOSTIC)
        self.assertNotIn("SECRET-KEY-VALUE", self.homing.STORE_DIAGNOSTIC)
