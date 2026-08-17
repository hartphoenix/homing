"""Create deterministic, local-only data for reviewing Homing changes."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import Profile, User
from projects.models import Lead, LeadComment, LeadInterest, Project, ProjectMembership


DEMO_PASSWORD = "homing-demo-password"


class Command(BaseCommand):
    help = "Idempotently seed a local review project and demo collaborators."

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("seed_demo_data is local-only and requires DJANGO_DEBUG=true")

        people = (
            ("alex@demo.example.test", "Alex", ProjectMembership.Role.OWNER),
            ("blair@demo.example.test", "Blair", ProjectMembership.Role.EDITOR),
            ("casey@demo.example.test", "Casey", ProjectMembership.Role.VIEWER),
        )
        users = {}
        for email, nickname, role in people:
            user, _ = User.objects.get_or_create(email=email)
            # Predictable credentials are intentional here. The command cannot
            # run with production settings and these addresses cannot receive mail.
            user.set_password(DEMO_PASSWORD)
            user.save(update_fields=["password", "updated_at"])
            Profile.objects.update_or_create(user=user, defaults={"display_name": nickname})
            users[nickname] = user

        project, _ = Project.objects.update_or_create(
            slug="demo-housing-search",
            defaults={
                "name": "Demo housing search",
                "description": "Local-only review data for Homing interface changes.",
                "prompt": "Find a quiet, transit-friendly two-bedroom with outdoor space.",
                "criteria": {"bedrooms": 2, "outdoor_space": True},
                "creator": users["Alex"],
            },
        )
        for _, nickname, role in people:
            ProjectMembership.objects.update_or_create(
                project=project,
                user=users[nickname],
                defaults={"role": role},
            )

        lead_specs = (
            {
                "source_listing_id": "demo-1",
                "canonical_url": "https://example.test/listings/garden-flat",
                "title": "Garden flat near the train",
                "summary": "Two bedrooms, private patio, and a short walk to transit.",
                "location": "Crown Heights",
                "price_display": "$3,200 / month",
                "availability": "September 1",
                "housing_type": Lead.HousingType.ENTIRE,
                "date_confidence": Lead.DateConfidence.STRONG,
            },
            {
                "source_listing_id": "demo-2",
                "canonical_url": "https://example.test/listings/sunny-brownstone",
                "title": "Sunny brownstone floor-through",
                "summary": "Promising layout; laundry and move-in date still need verification.",
                "location": "Bed-Stuy",
                "price_display": "$2,950 / month",
                "availability": "Early September",
                "housing_type": Lead.HousingType.ENTIRE,
                "date_confidence": Lead.DateConfidence.VERIFY,
            },
            {
                "source_listing_id": "demo-3",
                "canonical_url": "https://example.test/listings/park-view",
                "title": "Park-view apartment",
                "summary": "Excellent location, but price and exact dates are not listed.",
                "location": "Prospect Heights",
                "price_display": "",
                "availability": "",
                "housing_type": Lead.HousingType.UNKNOWN,
                "date_confidence": Lead.DateConfidence.UNKNOWN,
            },
            {
                "source_listing_id": "demo-4",
                "canonical_url": "https://example.test/listings/expired-loft",
                "title": "Expired loft listing",
                "summary": "A sample item in shared trash.",
                "location": "Bushwick",
                "price_display": "$3,500 / month",
                "availability": "No longer available",
                "housing_type": Lead.HousingType.ENTIRE,
                "date_confidence": Lead.DateConfidence.VERIFY,
            },
        )
        leads = {}
        for spec in lead_specs:
            identity = spec["source_listing_id"]
            lead, _ = Lead.objects.update_or_create(
                project=project,
                source="Demo listings",
                source_listing_id=identity,
                defaults={**spec, "creator": users["Alex"]},
            )
            leads[identity] = lead

        LeadInterest.objects.get_or_create(lead=leads["demo-1"], user=users["Alex"])
        LeadInterest.objects.get_or_create(lead=leads["demo-1"], user=users["Blair"])
        LeadInterest.objects.get_or_create(lead=leads["demo-2"], user=users["Casey"])
        LeadComment.objects.get_or_create(
            lead=leads["demo-1"],
            author=users["Alex"],
            body="The patio looks large enough for a small table.",
        )
        LeadComment.objects.get_or_create(
            lead=leads["demo-1"],
            author=users["Blair"],
            body="I like this one. I asked whether utilities are included.",
        )
        LeadComment.objects.get_or_create(
            lead=leads["demo-2"],
            author=users["Casey"],
            body="The listing says laundry is nearby, not in the building.",
        )

        trashed = leads["demo-4"]
        trashed.status = Lead.Status.TRASHED
        trashed.trashed_by = users["Blair"]
        trashed.trashed_at = trashed.trashed_at or timezone.now()
        trashed.save(update_fields=["status", "trashed_by", "trashed_at", "updated_at"])
        LeadComment.objects.get_or_create(
            lead=trashed,
            author=users["Blair"],
            body="Listing expired before anyone could schedule a viewing.",
        )

        self.stdout.write(self.style.SUCCESS("Demo data ready at /projects/demo-housing-search/"))
        self.stdout.write("Demo users: Alex, Blair, and Casey; password: homing-demo-password")
