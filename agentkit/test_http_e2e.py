"""End-to-end over real HTTP, from the served artifact.

Every other suite here uses the Django test client, which disables CSRF and
never touches the packaging layer. Two release blockers survived four agents and
a green suite because of exactly that gap: the API rejected every write with a
CSRF 403, and the installer refused the very artifact `/agent/` serves. These
tests run against a live server over a socket, and install from the zip a real
user downloads.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

from django.contrib.auth import get_user_model
from django.test import LiveServerTestCase, Client, TestCase
from django.urls import reverse

from accounts.models import AgentLink
from projects.models import Project, ProjectMembership

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def post_json(url, payload):
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


class CsrfDoesNotBlockTheApi(LiveServerTestCase):
    """A bearer credential is not ambient authority, so CSRF must not apply.

    Regression for the defect that made every write 403 in production while the
    whole test suite stayed green.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="e2e@example.test", password="e2e-pw-pw-123456")
        self.project = Project.objects.create(name="E2E", slug="e2e", creator=self.user)
        ProjectMembership.objects.create(
            project=self.project, user=self.user, role=ProjectMembership.Role.OWNER)

    def test_unauthenticated_pairing_start_is_not_csrf_blocked(self):
        status, body = post_json(self.live_server_url + "/api/v1/agent-link",
                                 {"agent_label": "over http"})
        self.assertEqual(status, 201, body)
        self.assertIn("device_code", body)
        self.assertEqual(len(body["user_code"]), 6)

    def test_full_pairing_and_write_over_http(self):
        base = self.live_server_url
        status, start = post_json(base + "/api/v1/agent-link", {"agent_label": "over http"})
        self.assertEqual(status, 201, start)

        status, pending = post_json(base + "/api/v1/agent-link/token",
                                    {"device_code": start["device_code"]})
        self.assertEqual(status, 400)
        self.assertEqual(pending["error"]["code"], "authorization_pending")

        web = Client()
        web.force_login(self.user)
        web.post(reverse("tracker:agent-link"),
                 {"code": start["user_code"], "action": "approve"})

        link = AgentLink.objects.get(user_code=start["user_code"])
        link.last_polled_at = None
        link.save(update_fields=["last_polled_at"])

        status, issued = post_json(base + "/api/v1/agent-link/token",
                                   {"device_code": start["device_code"]})
        self.assertEqual(status, 200, issued)
        token = issued["token"]
        self.assertNotIn("leads:destroy", issued["scopes"])

        # The write path is what CSRF was silently killing.
        request = urllib.request.Request(
            "%s/api/v1/projects/%s/search-runs" % (base, self.project.pk),
            data=json.dumps({"agent_label": "over http"}).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + token,
                     "Idempotency-Key": "e2e-http-1"})
        with urllib.request.urlopen(request, timeout=30) as response:
            self.assertEqual(response.status, 201)

    def test_session_unsafe_request_still_needs_csrf(self):
        """Exempting bearer must not exempt the ambient session cookie."""
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post("/api/v1/projects",
                               data=json.dumps({"name": "x", "prompt": "y"}),
                               content_type="application/json")
        self.assertEqual(response.status_code, 403)

    def test_bearer_write_passes_with_csrf_checks_enforced(self):
        from accounts.services.tokens import create_agent_token
        from projects.services.authorization import SCOPES
        _token, raw = create_agent_token(
            user=self.user, name="csrf", scopes=sorted(SCOPES), project_ids=[], expires_at=None)
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            "/api/v1/projects/%s/search-runs" % self.project.pk,
            data=json.dumps({"agent_label": "bearer"}), content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + raw, HTTP_IDEMPOTENCY_KEY="csrf-bearer-1")
        self.assertEqual(response.status_code, 201, response.content)


class ServedArtifactInstalls(LiveServerTestCase):
    """The installer must accept the exact bytes /agent/ serves.

    `/agent/` bakes the origin into every script at serve time; the installer
    used to refuse any script without the placeholder, which made the only
    documented install path impossible.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="homing-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def fetch(self, path):
        with urllib.request.urlopen(self.live_server_url + path, timeout=30) as response:
            return response.read()

    def test_served_scripts_carry_this_origin_not_the_placeholder(self):
        body = self.fetch("/agent/pkg/scripts/homing.py").decode()
        self.assertNotIn("__HOMING_ORIGIN__", body)
        self.assertIn(self.live_server_url, body)

    def test_installer_accepts_the_served_archive(self):
        archive = os.path.join(self.tmp, "kit.zip")
        with open(archive, "wb") as handle:
            handle.write(self.fetch("/agent/pkg/homing-agent-kit-1.zip"))
        extracted = os.path.join(self.tmp, "kit")
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extracted)

        root = os.path.join(self.tmp, "install")
        plan = {
            "schema": 1, "origin": self.live_server_url, "isolation_rung": 3,
            "runtime": {"kind": "none"},
            "scheduler": {"kind": "none"},
            "secret_store": {"kind": "file", "path": os.path.join(root, "token")},
            "paths": {"config": os.path.join(root, "config"),
                      "state": os.path.join(root, "state"),
                      "logs": os.path.join(root, "logs"),
                      "skill": os.path.join(root, "skills")},
            "notes": {"egress_class": "datacenter"},
            "sources": {"schema": 1, "allowed_hosts": ["example.test"], "sources": [{
                "slug": "example-test", "lane": "example-test:html", "channel": "html",
                "tier": "sanctioned", "url_template": "https://example.test/rent",
                "permitted_by": "robots.txt allow, checked 2026-08-18",
                "id_rule": "path_segment:-1",
                "listing_url_pattern": "^https://example\\\\.test/rent/",
                "fingerprint": {"listing_selector": "ld+json:RealEstateListing",
                                "min_ok_bytes": 1024},
                "egress_class_measured": "datacenter",
                "status": "ok", "next_eligible": None}]},
            "lanes": ["example-test:html"],
        }
        plan_path = os.path.join(self.tmp, "plan.json")
        with open(plan_path, "w") as handle:
            json.dump(plan, handle)

        proc = subprocess.run(
            [sys.executable, os.path.join(extracted, "scripts", "install.py"),
             "--config", plan_path],
            capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0,
                         "install from the served archive failed:\n%s\n%s"
                         % (proc.stdout, proc.stderr))

        installed = os.path.join(root, "config", "bin", "homing.py")
        self.assertTrue(os.path.isfile(installed))
        with open(installed) as handle:
            body = handle.read()
        self.assertNotIn("__HOMING_ORIGIN__", body)
        self.assertIn(self.live_server_url, body)

    def test_installer_refuses_an_archive_baked_for_a_different_origin(self):
        extracted = os.path.join(self.tmp, "other")
        os.makedirs(os.path.join(extracted, "scripts"))
        archive = os.path.join(self.tmp, "kit2.zip")
        with open(archive, "wb") as handle:
            handle.write(self.fetch("/agent/pkg/homing-agent-kit-1.zip"))
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extracted)
        for name in ("homing.py", "sources.py"):
            path = os.path.join(extracted, "scripts", name)
            with open(path) as handle:
                body = handle.read()
            with open(path, "w") as handle:
                handle.write(body.replace(self.live_server_url, "https://elsewhere.example"))

        root = os.path.join(self.tmp, "install2")
        plan_path = os.path.join(self.tmp, "plan2.json")
        with open(plan_path, "w") as handle:
            json.dump({"schema": 1, "origin": self.live_server_url, "isolation_rung": 3,
                       "runtime": {"kind": "none"}, "scheduler": {"kind": "none"},
                       "secret_store": {"kind": "file", "path": os.path.join(root, "token")},
                       "paths": {"config": os.path.join(root, "config"),
                                 "state": os.path.join(root, "state"),
                                 "logs": os.path.join(root, "logs"),
                                 "skill": os.path.join(root, "skills")},
                       "sources": {"schema": 1, "allowed_hosts": ["example.test"], "sources": [{
                           "slug": "example-test", "lane": "example-test:html",
                           "channel": "html", "tier": "sanctioned",
                           "url_template": "https://example.test/rent",
                           "permitted_by": "robots allow",
                           "id_rule": "path_segment:-1",
                           "listing_url_pattern": "^https://example\\\\.test/rent/",
                           "fingerprint": {"listing_selector": "ld+json:RealEstateListing",
                                           "min_ok_bytes": 1024},
                           "egress_class_measured": "datacenter",
                           "status": "ok", "next_eligible": None}]},
                       "lanes": ["example-test:html"]}, handle)

        proc = subprocess.run(
            [sys.executable, os.path.join(extracted, "scripts", "install.py"),
             "--config", plan_path],
            capture_output=True, text=True, timeout=180)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("elsewhere.example", proc.stdout + proc.stderr)


class RunCreateIsNotBucketedByHour(TestCase):
    """Two run-creates must not collide just because they share a UTC hour."""

    def test_distinct_keys(self):
        script = os.path.join(REPO, "agentkit", "package", "scripts", "homing.py")
        with open(script) as handle:
            body = handle.read()
        self.assertNotIn('time.strftime("%Y-%m-%dT%H", time.gmtime())', body)
        self.assertIn("--idempotency-key", body)
