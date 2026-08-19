from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import AgentToken, Profile, User
from projects.models import Project, ProjectMembership, SearchRun, SourcePlanReview
from projects.services.authorization import SCOPES
from tracker.agent_guidance import build_agent_prompt


class AgentSetupPresentationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner@example.com", "owner-password")
        Profile.objects.create(user=self.owner, display_name="Owner")
        self.project = Project.objects.create(
            name="September search", slug="september-search", creator=self.owner
        )
        ProjectMembership.objects.create(
            project=self.project, user=self.owner, role=ProjectMembership.Role.OWNER
        )
        self.client.force_login(self.owner)

    def test_setup_prompt_and_other_connection_copy_are_named_consistently(self):
        response = self.client.get(reverse("tracker:agent-setup"))

        self.assertContains(response, "Copy the prompt")
        self.assertNotContains(response, "Copy the instruction")
        self.assertContains(response, "When that command asks for the access key in your terminal")
        self.assertNotContains(response, "hand it over")
        self.assertNotContains(response, "<h3>Connections</h3>")

    def test_repair_prompt_is_origin_bound_and_contains_no_user_content(self):
        prompt = build_agent_prompt("https://search.example.test/api/v1", repair=True)
        self.assertIn("https://search.example.test/agent/", prompt)
        self.assertIn("existing installation", prompt)
        self.assertIn("open source-plan reviews", prompt)
        self.assertIn("current project prompts", prompt)
        self.assertIn("worker-wide source union", prompt)
        self.assertIn("avoid expensive discovery", prompt)
        self.assertIn("without dropping coverage", prompt)
        self.assertIn("self-test", prompt)
        self.assertIn("one on-demand check", prompt)
        self.assertIn("Resolve each review only after", prompt)
        self.assertIn("one plain human question at a time", prompt)
        self.assertNotIn("September search", prompt)
        self.assertNotIn("Sunny room", prompt)
        self.assertNotIn("/Users/", prompt)

    def test_connected_status_facts_and_recent_checks_are_compact_metadata(self):
        token = AgentToken.objects.create(
            user=self.owner,
            name="Search assistant",
            token_prefix="homing_test",
            digest="a" * 64,
            scopes=sorted(SCOPES),
            expires_at=timezone.now() + timedelta(days=30),
            expected_cadence_minutes=1440,
            environment_note="Mac",
        )
        SearchRun.objects.create(
            project=self.project,
            user=self.owner,
            agent_token=token,
            prompt_snapshot="Find a room",
            status=SearchRun.Status.COMPLETED,
            started_at=timezone.now(),
            completed_at=timezone.now(),
        )

        response = self.client.get(reverse("tracker:agent-setup"))

        self.assertContains(response, "Searching:")
        self.assertContains(response, "How often:")
        self.assertContains(response, "Where it runs:")
        self.assertContains(response, "class=\"check-metadata\"")
        self.assertContains(response, "aria-hidden=\"true\">·</span>")

    def test_open_review_has_site_banner_and_server_authored_repair_prompt(self):
        SourcePlanReview.objects.create(
            project=self.project,
            user=self.owner,
            observed_prompt_revision=self.project.prompt_revision,
        )

        project_page = self.client.get(reverse("tracker:project-list"))
        self.assertContains(project_page, "Your installed source plan needs review")
        self.assertContains(project_page, "1 active search")
        self.assertContains(project_page, reverse("tracker:agent-setup"))

        response = self.client.get(reverse("tracker:agent-setup"))
        self.assertContains(response, "Copy the repair prompt")
        self.assertContains(response, "existing installation")
        self.assertContains(response, "worker-wide source union")
        self.assertContains(response, "one on-demand check")
        prompt = response.context["source_review_prompt"]
        self.assertNotIn(self.project.name, prompt)
        self.assertNotIn("Sunny room", prompt)
        self.assertNotIn("/Users/", prompt)
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_resolved_or_inaccessible_review_does_not_show_banner(self):
        review = SourcePlanReview.objects.create(
            project=self.project,
            user=self.owner,
            observed_prompt_revision=self.project.prompt_revision,
            status=SourcePlanReview.Status.RESOLVED,
            resolved_prompt_revision=self.project.prompt_revision,
        )
        response = self.client.get(reverse("tracker:project-list"))
        self.assertNotContains(response, "Your installed source plan needs review")

        review.status = SourcePlanReview.Status.OPEN
        review.save(update_fields=["status"])
        ProjectMembership.objects.filter(project=self.project, user=self.owner).delete()
        response = self.client.get(reverse("tracker:project-list"))
        self.assertNotContains(response, "Your installed source plan needs review")
