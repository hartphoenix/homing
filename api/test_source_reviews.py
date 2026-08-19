import json

from django.test import Client, TestCase

from accounts.models import User
from accounts.services.tokens import create_agent_token
from projects.models import AuditEvent, Project, ProjectChange, ProjectMembership, SourcePlanReview


class SourcePlanReviewApiTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner-review@example.com", password="password")
        self.collaborator = User.objects.create_user("collab-review@example.com", password="password")
        self.project = Project.objects.create(
            name="Search", slug="source-review-search", creator=self.owner,
            prompt="find housing", prompt_revision=4,
        )
        ProjectMembership.objects.create(project=self.project, user=self.owner, role="owner")
        ProjectMembership.objects.create(project=self.project, user=self.collaborator, role="viewer")
        self.owner_token, self.owner_raw = create_agent_token(
            user=self.owner, name="owner agent", scopes={"projects:read", "runs:write"},
            project_ids=[str(self.project.pk)],
        )

    def api(self, method, path, raw=None, data=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {raw}"} if raw else {}
        return getattr(Client(), method)(
            path, data=json.dumps(data) if data is not None else None,
            content_type="application/json", **headers,
        )

    def test_report_is_strict_idempotent_and_audited(self):
        response = self.api(
            "post", f"/api/v1/projects/{self.project.pk}/source-plan-review",
            self.owner_raw, {"prompt_revision": 4},
        )
        self.assertEqual(response.status_code, 201)
        review_id = response.json()["id"]
        self.assertEqual(SourcePlanReview.objects.filter(status="open").count(), 1)
        self.assertFalse(ProjectChange.objects.filter(event_type__startswith="source_plan_review.").exists())
        self.assertTrue(AuditEvent.objects.filter(action="source_plan_review.opened").exists())

        repeated = self.api(
            "post", f"/api/v1/projects/{self.project.pk}/source-plan-review",
            self.owner_raw, {"prompt_revision": 4},
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json()["id"], review_id)
        self.assertEqual(SourcePlanReview.objects.filter(status="open").count(), 1)

    def test_stale_and_non_integer_or_free_text_requests_are_rejected(self):
        url = f"/api/v1/projects/{self.project.pk}/source-plan-review"
        for value in (True, 4.0, "4"):
            with self.subTest(value=value):
                self.assertEqual(self.api("post", url, self.owner_raw, {"prompt_revision": value}).status_code, 422)
        self.assertEqual(self.api("post", url, self.owner_raw, {"prompt_revision": 4, "reason": "x"}).status_code, 422)
        self.assertEqual(self.api("post", url, self.owner_raw, {"prompt_revision": 3}).status_code, 409)

    def test_list_is_project_restricted_and_resolve_allows_another_token(self):
        opened = self.api(
            "post", f"/api/v1/projects/{self.project.pk}/source-plan-review",
            self.owner_raw, {"prompt_revision": 4},
        ).json()
        listed = self.api("get", "/api/v1/me/source-plan-reviews?status=open", self.owner_raw)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([row["id"] for row in listed.json()["items"]], [opened["id"]])

        other_token, other_raw = create_agent_token(
            user=self.owner, name="repair agent", scopes={"runs:write"},
            project_ids=[str(self.project.pk)],
        )
        resolved = self.api(
            "post",
            f"/api/v1/projects/{self.project.pk}/source-plan-review/{opened['id']}/resolve",
            other_raw, {"prompt_revision": 4},
        )
        self.assertEqual(resolved.status_code, 200)
        self.assertEqual(resolved.json()["status"], "resolved")
        self.assertFalse(ProjectChange.objects.filter(event_type__startswith="source_plan_review.").exists())
        self.assertEqual(AuditEvent.objects.filter(action="source_plan_review.resolved").count(), 1)
        self.assertEqual(SourcePlanReview.objects.get(pk=opened["id"]).resolving_agent_token_id, other_token.pk)

    def test_collaborator_cannot_resolve_owner_review_and_token_deletion_is_safe(self):
        opened = self.api(
            "post", f"/api/v1/projects/{self.project.pk}/source-plan-review",
            self.owner_raw, {"prompt_revision": 4},
        ).json()
        collab_token, collab_raw = create_agent_token(
            user=self.collaborator, name="collab", scopes={"runs:write"},
            project_ids=[str(self.project.pk)],
        )
        response = self.api(
            "post",
            f"/api/v1/projects/{self.project.pk}/source-plan-review/{opened['id']}/resolve",
            collab_raw, {"prompt_revision": 4},
        )
        self.assertEqual(response.status_code, 404)
        self.owner_token.delete()
        review = SourcePlanReview.objects.get(pk=opened["id"])
        self.assertIsNone(review.reporting_agent_token_id)
        collab_token.delete()

    def test_a_second_stale_install_can_reopen_the_same_revision(self):
        opened = self.api(
            "post", f"/api/v1/projects/{self.project.pk}/source-plan-review",
            self.owner_raw, {"prompt_revision": 4},
        ).json()
        resolved = self.api(
            "post",
            f"/api/v1/projects/{self.project.pk}/source-plan-review/{opened['id']}/resolve",
            self.owner_raw, {"prompt_revision": 4},
        )
        self.assertEqual(resolved.status_code, 200)

        reopened = self.api(
            "post", f"/api/v1/projects/{self.project.pk}/source-plan-review",
            self.owner_raw, {"prompt_revision": 4},
        )
        self.assertEqual(reopened.status_code, 201)
        self.assertNotEqual(reopened.json()["id"], opened["id"])
        self.assertEqual(reopened.json()["status"], "open")

    def test_resolution_requires_the_review_to_observe_the_current_revision(self):
        opened = self.api(
            "post", f"/api/v1/projects/{self.project.pk}/source-plan-review",
            self.owner_raw, {"prompt_revision": 4},
        ).json()
        self.project.prompt_revision = 5
        self.project.save(update_fields=["prompt_revision", "updated_at"])

        stale = self.api(
            "post",
            f"/api/v1/projects/{self.project.pk}/source-plan-review/{opened['id']}/resolve",
            self.owner_raw, {"prompt_revision": 5},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"]["code"], "source_plan_review_stale")

        refreshed = self.api(
            "post", f"/api/v1/projects/{self.project.pk}/source-plan-review",
            self.owner_raw, {"prompt_revision": 5},
        )
        self.assertEqual(refreshed.status_code, 200)
        resolved = self.api(
            "post",
            f"/api/v1/projects/{self.project.pk}/source-plan-review/{opened['id']}/resolve",
            self.owner_raw, {"prompt_revision": 5},
        )
        self.assertEqual(resolved.status_code, 200)
