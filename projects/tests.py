
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.test import TestCase

from accounts.models import User
from projects.models import Lead, Project, ProjectMembership
from projects.services.authorization import authorize_project
from projects.services.mutations import PromptRevisionConflict, trash_lead, update_project_prompt


class ProjectFoundationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner@example.com", "secret")
        self.viewer = User.objects.create_user("viewer@example.com", "secret")
        self.other = User.objects.create_user("other@example.com", "secret")
        self.project = Project.objects.create(name="Search", slug="search", creator=self.owner)
        ProjectMembership.objects.create(project=self.project, user=self.owner, role="owner")
        ProjectMembership.objects.create(project=self.project, user=self.viewer, role="viewer")
        self.lead = Lead.objects.create(project=self.project, creator=self.owner, source="source", canonical_url="https://example.test/listing/1", title="A lead")

    def test_inaccessible_projects_are_404_and_role_failures_are_403(self):
        with self.assertRaises(Http404):
            authorize_project(self.project, self.other)
        with self.assertRaises(PermissionDenied):
            authorize_project(self.project, self.viewer, minimum_role="editor")

    def test_prompt_update_is_versioned_and_rejects_stale_revision(self):
        revision = update_project_prompt(self.project, editor=self.owner, prompt="new", criteria={"x": 1}, expected_revision=0)
        self.assertEqual(revision.revision, 1)
        self.project.refresh_from_db()
        self.assertEqual(self.project.prompt_revision, 1)
        with self.assertRaises(PromptRevisionConflict):
            update_project_prompt(self.project, editor=self.owner, prompt="stale", criteria={}, expected_revision=0)

    def test_trash_requires_reason_and_is_reversible(self):
        with self.assertRaises(ValueError):
            trash_lead(self.lead, actor=self.owner, reason="")
        trashed = trash_lead(self.lead, actor=self.owner, reason="Dates contradicted")
        self.assertEqual(trashed.status, Lead.Status.TRASHED)
