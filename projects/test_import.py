import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.test import TestCase

from accounts.models import User
from projects.models import Lead, LeadInterest, Project, ProjectMembership, PromptRevision


class BootstrapSubletProjectTests(TestCase):
    def run_bootstrap(self, **kwargs):
        call_command(
            "bootstrap_sublet_project",
            email="owner@example.com",
            password="not-printed",
            **kwargs,
        )

    def test_bootstrap_is_idempotent_and_preserves_all_listing_fields(self):
        self.run_bootstrap()
        project = Project.objects.get(slug="september-2026-sublet")
        self.assertEqual(Lead.objects.filter(project=project).count(), 25)
        self.assertEqual(ProjectMembership.objects.filter(project=project, role="owner").count(), 1)
        self.assertEqual(PromptRevision.objects.filter(project=project).count(), 1)
        lead = Lead.objects.get(project=project, source_listing_id="leasebreak-398175")
        self.assertEqual(lead.canonical_url, "https://www.leasebreak.com/short-term-rental-details/398175/1869-madison-street")
        self.assertEqual(lead.availability, "Aug 31–Sep 30; 1-month minimum")
        self.assertEqual(lead.housing_type, "shared")
        self.assertEqual(lead.date_confidence, "strong")
        self.assertIn("unknowns", lead.attributes)
        revision = project.prompt_revision

        self.run_bootstrap()
        self.assertEqual(Lead.objects.filter(project=project).count(), 25)
        self.assertEqual(PromptRevision.objects.filter(project=project).count(), 1)
        project.refresh_from_db()
        self.assertEqual(project.prompt_revision, revision)

    def test_legacy_interest_and_trash_are_mapped_and_unknown_ids_reported(self):
        with TemporaryDirectory() as temporary_directory:
            tmp_path = Path(temporary_directory)
            state_path = tmp_path / "local-storage.json"
            state_path.write_text(
                json.dumps(
                    {
                        "interested": {"leasebreak-398175": True, "gone-interest": True},
                        "trashed": {"leasebreak-401831": True, "gone-trash": True},
                    }
                ),
                encoding="utf-8",
            )
            report_path = tmp_path / "legacy-report.json"
            self.run_bootstrap(legacy_state=state_path, legacy_report=report_path)

            user = User.objects.get(email="owner@example.com")
            project = Project.objects.get(slug="september-2026-sublet")
            interested = Lead.objects.get(project=project, source_listing_id="leasebreak-398175")
            trashed = Lead.objects.get(project=project, source_listing_id="leasebreak-401831")
            self.assertTrue(LeadInterest.objects.filter(lead=interested, user=user).exists())
            self.assertEqual(trashed.status, Lead.Status.TRASHED)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["unknown_interested_ids"], ["gone-interest"])
            self.assertEqual(report["unknown_trashed_ids"], ["gone-trash"])

            self.run_bootstrap(legacy_state=state_path)
            self.assertEqual(Lead.objects.filter(project=project).count(), 25)
            self.assertEqual(LeadInterest.objects.filter(user=user).count(), 1)
