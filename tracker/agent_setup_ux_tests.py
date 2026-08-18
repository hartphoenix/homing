from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import AgentToken, Profile, User
from projects.models import Project, ProjectMembership, SearchRun
from projects.services.authorization import SCOPES


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
