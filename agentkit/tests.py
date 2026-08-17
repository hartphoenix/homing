"""Route, header and integrity tests for the public agent kit.

``SimpleTestCase`` rather than ``TestCase`` on purpose: these routes must never touch the
database or a session, and ``SimpleTestCase`` turns that from a claim into an assertion.
"""

import hashlib
import io
import json
import zipfile

from django.test import Client, SimpleTestCase, override_settings

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
