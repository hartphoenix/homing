import json
from datetime import timedelta

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from accounts.services.tokens import create_agent_token, digest_token
from projects.models import IdempotencyKey, Lead, LeadInterest, Project, ProjectInvitation, ProjectMembership


class ApiSecurityTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner@example.com", password="correct horse battery staple")
        self.viewer = User.objects.create_user("viewer@example.com", password="correct horse battery staple")
        self.other = User.objects.create_user("other@example.com", password="correct horse battery staple")
        self.project = Project.objects.create(
            name="Search", slug="search", creator=self.owner, prompt="find housing"
        )
        self.other_project = Project.objects.create(
            name="Other", slug="other", creator=self.other, prompt="other"
        )
        ProjectMembership.objects.create(project=self.project, user=self.owner, role="owner")
        ProjectMembership.objects.create(project=self.project, user=self.viewer, role="viewer")
        ProjectMembership.objects.create(project=self.other_project, user=self.other, role="owner")
        self.lead = Lead.objects.create(
            project=self.project,
            creator=self.owner,
            source="example",
            source_listing_id="1",
            canonical_url="https://example.test/1",
            title="A lead",
        )

    def token(self, user, scopes, project_ids=None):
        token, raw = create_agent_token(
            user=user,
            name="test",
            scopes=scopes,
            project_ids=project_ids,
        )
        return token, raw

    def api(self, method, path, raw=None, **kwargs):
        headers = kwargs.pop("headers", {})
        if raw:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {raw}"
        kwargs.setdefault("content_type", "application/json")
        return getattr(Client(), method)(path, **kwargs, **headers)

    def test_invalid_bearer_never_falls_back_to_session(self):
        client = Client()
        client.force_login(self.owner)
        response = client.get(
            f"/api/v1/projects/{self.project.pk}",
            HTTP_AUTHORIZATION="Bearer definitely-invalid",
        )
        self.assertEqual(response.status_code, 401)

    def test_bearer_scope_and_project_restriction_survive_session_cookie(self):
        _, raw = self.token(self.owner, {"leads:read"}, [str(self.project.pk)])
        client = Client()
        client.force_login(self.owner)
        response = client.post(
            f"/api/v1/projects/{self.project.pk}/leads",
            data=json.dumps({"source": "x", "url": "https://example.test/x", "title": "x"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {raw}",
        )
        self.assertEqual(response.status_code, 403)

    def test_restricted_token_does_not_list_other_projects(self):
        ProjectMembership.objects.create(project=self.other_project, user=self.owner, role="viewer")
        _, raw = self.token(self.owner, {"projects:read"}, [str(self.project.pk)])
        response = self.api("get", "/api/v1/me/projects", raw)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json()["items"]], [str(self.project.pk)])

    def test_cross_project_lead_id_is_not_resolvable(self):
        _, raw = self.token(self.owner, {"leads:read"}, [str(self.project.pk)])
        response = self.api(
            "get",
            f"/api/v1/projects/{self.project.pk}/leads/{self.lead.pk}",
            raw,
        )
        self.assertEqual(response.status_code, 200)
        response = self.api(
            "get",
            f"/api/v1/projects/{self.other_project.pk}/leads/{self.lead.pk}",
            raw,
        )
        self.assertEqual(response.status_code, 404)

    def test_interest_is_attributed_to_user_and_emits_change(self):
        _, raw = self.token(self.viewer, {"interest:write", "interest:read"}, [str(self.project.pk)])
        response = self.api(
            "put",
            f"/api/v1/projects/{self.project.pk}/leads/{self.lead.pk}/interest",
            raw,
        )
        self.assertEqual(response.status_code, 204)
        self.assertTrue(LeadInterest.objects.filter(lead=self.lead, user=self.viewer).exists())
        self.assertTrue(self.project.changes.filter(event_type="interest.set").exists())

    def test_viewer_cannot_write_leads_even_with_write_scope(self):
        _, raw = self.token(self.viewer, {"leads:read", "leads:write"}, [str(self.project.pk)])
        response = self.api(
            "post",
            f"/api/v1/projects/{self.project.pk}/leads",
            raw,
            data=json.dumps({"source": "x", "url": "https://example.test/x", "title": "x"}),
        )
        self.assertEqual(response.status_code, 403)
        response = self.api(
            "patch",
            f"/api/v1/projects/{self.project.pk}/leads/{self.lead.pk}",
            raw,
            data=json.dumps({"title": "changed"}),
            headers={"HTTP_IF_MATCH": '"1"'},
        )
        self.assertEqual(response.status_code, 403)

    def test_agent_cannot_manage_tokens_or_memberships(self):
        _, raw = self.token(self.owner, {"profile:read", "projects:read"}, [str(self.project.pk)])
        self.assertEqual(self.api("get", "/api/v1/auth/tokens", raw).status_code, 403)
        response = self.api(
            "patch",
            f"/api/v1/projects/{self.project.pk}/members",
            raw,
            data=json.dumps({"user_id": str(self.viewer.pk), "role": "editor"}),
        )
        self.assertEqual(response.status_code, 403)

    def test_invitation_token_mismatch_is_not_an_oracle(self):
        raw_invite = "invite-secret"
        ProjectInvitation.objects.create(
            project=self.project,
            invited_email="viewer@example.com",
            inviter=self.owner,
            role="viewer",
            token_digest=digest_token(raw_invite),
            expires_at=timezone.now() + timedelta(days=1),
        )
        client = Client()
        client.force_login(self.other)
        response = client.post(f"/api/v1/invitations/{raw_invite}/accept")
        self.assertEqual(response.status_code, 404)

    def test_password_exchange_is_database_throttled_with_retry_after(self):
        client = Client()
        payload = {"email": self.owner.email, "password": "wrong password"}
        responses = [
            client.post("/api/v1/auth/token", data=json.dumps(payload), content_type="application/json")
            for _ in range(6)
        ]
        self.assertEqual([response.status_code for response in responses[:5]], [401] * 5)
        self.assertEqual(responses[5].status_code, 429)
        self.assertGreaterEqual(int(responses[5]["Retry-After"]), 1)

    def test_successful_password_exchange_resets_throttle_buckets(self):
        client = Client()
        wrong = {"email": self.owner.email, "password": "wrong password"}
        for _ in range(4):
            self.assertEqual(
                client.post("/api/v1/auth/token", data=json.dumps(wrong), content_type="application/json").status_code,
                401,
            )
        good = {"email": self.owner.email, "password": "correct horse battery staple"}
        self.assertEqual(
            client.post("/api/v1/auth/token", data=json.dumps(good), content_type="application/json").status_code,
            200,
        )
        for _ in range(5):
            self.assertEqual(
                client.post("/api/v1/auth/token", data=json.dumps(wrong), content_type="application/json").status_code,
                401,
            )
        self.assertEqual(
            client.post("/api/v1/auth/token", data=json.dumps(wrong), content_type="application/json").status_code,
            429,
        )

    @override_settings(
        AUTH_PASSWORD_VALIDATORS=[
            {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 14}}
        ]
    )
    def test_registration_uses_configured_django_password_validators(self):
        response = Client().post(
            "/api/v1/auth/register",
            data=json.dumps({"email": "new@example.com", "password": "twelve chars"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("password", response.json()["error"]["message"].lower())

    def test_run_completion_replays_durable_idempotency_response(self):
        _, raw = self.token(self.owner, {"runs:write"}, [str(self.project.pk)])
        response = self.api(
            "post",
            f"/api/v1/projects/{self.project.pk}/search-runs",
            raw,
            data=json.dumps({"agent_label": "test"}),
            headers={"HTTP_IDEMPOTENCY_KEY": "create-run"},
        )
        run_id = response.json()["id"]
        response = self.api(
            "post",
            f"/api/v1/projects/{self.project.pk}/search-runs/{run_id}/claim",
            raw,
        )
        claim = response.json()["claim_token"]
        payload = {"claim_token": claim, "status": "completed", "summary": "done"}
        first = self.api(
            "post",
            f"/api/v1/projects/{self.project.pk}/search-runs/{run_id}/complete",
            raw,
            data=json.dumps(payload),
            headers={"HTTP_IDEMPOTENCY_KEY": "complete-run"},
        )
        second = self.api(
            "post",
            f"/api/v1/projects/{self.project.pk}/search-runs/{run_id}/complete",
            raw,
            data=json.dumps(payload),
            headers={"HTTP_IDEMPOTENCY_KEY": "complete-run"},
        )
        self.assertEqual(first.status_code, second.status_code)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(
            IdempotencyKey.objects.filter(key="complete-run").count(),
            1,
        )
        changed = dict(payload, summary="different")
        conflict = self.api(
            "post",
            f"/api/v1/projects/{self.project.pk}/search-runs/{run_id}/complete",
            raw,
            data=json.dumps(changed),
            headers={"HTTP_IDEMPOTENCY_KEY": "complete-run"},
        )
        self.assertEqual(conflict.status_code, 409)
