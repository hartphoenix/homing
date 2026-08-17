from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from accounts.models import User
from projects.models import Lead, Project, ProjectMembership


class DemoSeedTests(TestCase):
    @override_settings(DEBUG=True)
    def test_seed_is_idempotent_and_creates_review_states(self):
        call_command("seed_demo_data", verbosity=0)
        call_command("seed_demo_data", verbosity=0)

        project = Project.objects.get(slug="demo-housing-search")
        self.assertEqual(project.memberships.count(), 3)
        self.assertEqual(project.leads.count(), 4)
        self.assertEqual(project.leads.filter(status=Lead.Status.TRASHED).count(), 1)
        self.assertEqual(project.leads.get(source_listing_id="demo-1").interests.count(), 2)
        self.assertEqual(project.leads.get(source_listing_id="demo-1").comments.count(), 2)
        self.assertEqual(
            set(
                ProjectMembership.objects.filter(project=project).values_list(
                    "user__profile__display_name", flat=True
                )
            ),
            {"Alex", "Blair", "Casey"},
        )
        self.assertTrue(
            User.objects.get(email="alex@demo.example.test").check_password(
                "homing-demo-password"
            )
        )

    @override_settings(DEBUG=False)
    def test_seed_refuses_non_debug_settings(self):
        with self.assertRaises(CommandError):
            call_command("seed_demo_data", verbosity=0)
