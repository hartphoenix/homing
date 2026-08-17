"""Bootstrap the September project and import the original browser tracker data.

The command is deliberately safe to run from a cron job: records are matched by
the source's stable listing id (or canonical URL), and existing annotations are
never reset.  Passwords are accepted through an environment variable for cron
use; they are never included in command output.
"""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import Profile, User
from projects.models import Lead, LeadComment, LeadInterest, Project, ProjectMembership, PromptRevision, listing_identity_hash


DEFAULT_PROJECT_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "september_2026_project.json"
DEFAULT_LISTINGS = settings.BASE_DIR / "listings.js"


def _load_project_fixture(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommandError(f"Cannot read project fixture {path}: {exc}") from exc
    if not isinstance(value, dict) or not value.get("slug") or not value.get("prompt"):
        raise CommandError(f"Project fixture {path} must contain slug and prompt")
    return value


def _load_listings(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CommandError(f"Cannot read listings file {path}: {exc}") from exc
    marker = "window.SUBLET_LISTINGS"
    start = text.find("[", text.find(marker))
    if start < 0:
        raise CommandError(f"No {marker} array found in {path}")
    # listings.js intentionally stays convenient to edit as JavaScript and uses
    # unquoted object keys. Values are JSON-compatible, so quote only bare keys
    # rather than evaluating arbitrary JavaScript from a checked-in data file.
    array_text = re.sub(r"([\{,]\s*)([A-Za-z_$][\w$]*)(\s*:)", r'\1"\2"\3', text[start:])
    try:
        listings, _ = json.JSONDecoder().raw_decode(array_text)
    except json.JSONDecodeError as exc:
        raise CommandError(f"The listings array in {path} is not JSON-compatible: {exc}") from exc
    if not isinstance(listings, list) or not all(isinstance(item, dict) for item in listings):
        raise CommandError(f"The listings array in {path} must contain objects")
    return listings


def _price_amount(display: str) -> Decimal | None:
    """Return the first advertised dollar amount, retaining the display string too."""
    match = re.search(r"(?:\$|USD\s*)([0-9][0-9,]*(?:\.\d{1,2})?)", display or "", re.IGNORECASE)
    if not match:
        return None
    try:
        return Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None


def _legacy_ids(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {str(key) for key, enabled in value.items() if enabled}
    if isinstance(value, list):
        return {str(item) for item in value}
    return set()


def _load_legacy_state(path: Path) -> dict[str, set[str]]:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommandError(f"Cannot read legacy state {path}: {exc}") from exc
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CommandError(f"Legacy state {path} contains invalid JSON text: {exc}") from exc
    if not isinstance(raw, dict):
        raise CommandError("Legacy state must be an object with interested and/or trashed keys")
    return {key: _legacy_ids(raw.get(key, {})) for key in ("interested", "trashed")}


class Command(BaseCommand):
    help = """Idempotently create/update the September 2026 project and import listings.js.

Required for a new account: --email and --password, or BOOTSTRAP_EMAIL and
BOOTSTRAP_PASSWORD. For an existing account, omit the password to leave it
unchanged. Passwords are write-only and are never printed. Pass --legacy-state
the JSON exported from localStorage (the {interested, trashed} object) to import
the old browser annotations. Unknown legacy IDs are always reported; they are
never silently discarded. --dry-run performs validation and reports changes
without writing to the database.
"""

    def add_arguments(self, parser):
        parser.add_argument("--email", default=os.environ.get("BOOTSTRAP_EMAIL", ""))
        parser.add_argument("--password", default=os.environ.get("BOOTSTRAP_PASSWORD"))
        parser.add_argument("--listings", type=Path, default=DEFAULT_LISTINGS)
        parser.add_argument("--project-fixture", type=Path, default=DEFAULT_PROJECT_FIXTURE)
        parser.add_argument("--legacy-state", type=Path)
        parser.add_argument("--legacy-report", type=Path, help="Also write the unknown-ID report as JSON")
        parser.add_argument("--dry-run", action="store_true", help="Validate and report without writing")

    def handle(self, *args, **options):
        email = (options["email"] or "").strip().casefold()
        password = options["password"]
        if not email:
            raise CommandError("An email is required via --email or BOOTSTRAP_EMAIL")
        if not password and not User.objects.filter(email=email).exists():
            raise CommandError("A password is required when creating a new user (use --password or BOOTSTRAP_PASSWORD)")

        fixture = _load_project_fixture(options["project_fixture"])
        listings = _load_listings(options["listings"])
        legacy = _load_legacy_state(options["legacy_state"]) if options["legacy_state"] else {"interested": set(), "trashed": set()}

        if options["dry_run"]:
            user = User.objects.filter(email=email).first()
            project = Project.objects.filter(slug=fixture["slug"]).first()
            report = self._report(legacy, listings)
            self.stdout.write(f"dry-run: {len(listings)} listings; user={'existing' if user else 'new'}")
            self._write_report(report, options["legacy_report"])
            return

        with transaction.atomic():
            user, user_created = User.objects.get_or_create(email=email, defaults={})
            if user_created:
                user.set_password(password)
                user.save(update_fields=["password", "updated_at"])
            elif password:
                user.set_password(password)
                user.save(update_fields=["password", "updated_at"])
            Profile.objects.get_or_create(
                user=user,
                defaults={"display_name": email.partition("@")[0][:120] or "Member"},
            )

            project, project_created = Project.objects.get_or_create(
                slug=fixture["slug"],
                defaults={
                    "name": fixture["name"],
                    "description": fixture.get("description", ""),
                    "prompt": fixture["prompt"],
                    "criteria": fixture.get("criteria", {}),
                    "creator": user,
                    "prompt_revision": 1,
                },
            )
            if project_created:
                PromptRevision.objects.create(
                    project=project,
                    revision=1,
                    prompt=project.prompt,
                    criteria=project.criteria,
                    editor=user,
                )
            elif project.prompt != fixture["prompt"] or project.criteria != fixture.get("criteria", {}):
                revision = project.prompt_revision + 1
                project.prompt = fixture["prompt"]
                project.criteria = fixture.get("criteria", {})
                project.prompt_revision = revision
                project.save(update_fields=["prompt", "criteria", "prompt_revision", "updated_at"])
                PromptRevision.objects.update_or_create(
                    project=project,
                    revision=revision,
                    defaults={"prompt": project.prompt, "criteria": project.criteria, "editor": user},
                )
            ProjectMembership.objects.update_or_create(
                project=project, user=user, defaults={"role": ProjectMembership.Role.OWNER}
            )

            imported = self._import_listings(project, user, listings)
            report = self._apply_legacy(project, user, legacy, imported)

        self.stdout.write(
            f"bootstrap complete: user={'created' if user_created else 'updated'}, "
            f"project={'created' if project_created else 'updated'}, "
            f"listings created={imported['created']} updated={imported['updated']} total={len(listings)}"
        )
        self._write_report(report, options["legacy_report"])

    def _import_listings(self, project, user, listings):
        created = updated = 0
        by_legacy_id = {}
        url_counts: dict[str, int] = {}
        for listing in listings:
            raw_url = str(listing.get("url") or "").strip()
            url_counts[raw_url] = url_counts.get(raw_url, 0) + 1
        for item in listings:
            source = str(item.get("source") or "Unknown")[:160]
            source_listing_id = str(item.get("id") or "")[:200]
            source_url = str(item.get("url") or "").strip()
            if not source_url:
                raise CommandError(f"Listing {source_listing_id or item.get('title', '<untitled>')} has no URL")
            # Search-page records occasionally share a URL. Keep the exact
            # source URL for display/API use, while adding a stable fragment to
            # canonical identity so every source listing remains importable.
            url = source_url
            if url_counts[source_url] > 1 and source_listing_id:
                separator = "&" if "?" in source_url else "?"
                url = f"{source_url}{separator}sublet_listing={source_listing_id}"
            defaults = {
                "source": source,
                "source_listing_id": source_listing_id,
                "canonical_url": url,
                "source_url": source_url,
                "title": str(item.get("title") or "Untitled")[:500],
                "summary": str(item.get("summary") or ""),
                "location": str(item.get("location") or "")[:500],
                "price_display": str(item.get("price") or "")[:120],
                "price_amount": _price_amount(str(item.get("price") or "")),
                "availability": str(item.get("dates") or "")[:500],
                "housing_type": item.get("type") if item.get("type") in {"entire", "shared"} else Lead.HousingType.UNKNOWN,
                "date_confidence": item.get("dateFit") if item.get("dateFit") in {"strong", "verify"} else Lead.DateConfidence.UNKNOWN,
                "park_notes": str(item.get("parks") or "")[:1000],
                "attributes": {
                    "legacy_id": source_listing_id,
                    "unknowns": item.get("unknowns") if isinstance(item.get("unknowns"), list) else [],
                    "added": item.get("added", ""),
                },
                "verification_notes": "\n".join(str(value) for value in item.get("unknowns", []) if value),
                "creator": user,
            }
            identity = listing_identity_hash(url)
            lead = None
            if source_listing_id:
                lead = project.leads.filter(source=source, source_listing_id=source_listing_id).first()
            if lead is None and not source_listing_id and identity:
                lead = project.leads.filter(identity_hash=identity).first()
            if lead is None:
                lead = Lead.objects.create(project=project, **defaults)
                created += 1
            else:
                changed = any(getattr(lead, key) != value for key, value in defaults.items() if key != "creator")
                if changed:
                    for key, value in defaults.items():
                        if key != "creator":
                            setattr(lead, key, value)
                    lead.revision += 1
                    lead.save()
                    updated += 1
            if source_listing_id:
                by_legacy_id[source_listing_id] = lead
        return {"created": created, "updated": updated, "by_id": by_legacy_id}

    def _report(self, legacy, listings):
        known = {str(item.get("id")) for item in listings if item.get("id")}
        return {
            "interested_ids": sorted(legacy["interested"]),
            "trashed_ids": sorted(legacy["trashed"]),
            "unknown_interested_ids": sorted(legacy["interested"] - known),
            "unknown_trashed_ids": sorted(legacy["trashed"] - known),
        }

    def _apply_legacy(self, project, user, legacy, imported):
        for legacy_id in legacy["interested"]:
            lead = imported["by_id"].get(legacy_id)
            if lead:
                LeadInterest.objects.get_or_create(lead=lead, user=user)
        for legacy_id in legacy["trashed"]:
            lead = imported["by_id"].get(legacy_id)
            if lead and lead.status != Lead.Status.TRASHED:
                lead.status = Lead.Status.TRASHED
                lead.trashed_by = user
                lead.trashed_at = timezone.now()
                lead.revision += 1
                lead.save(update_fields=["status", "trashed_by", "trashed_at", "revision", "updated_at"])
                LeadComment.objects.create(
                    lead=lead,
                    author=user,
                    body="Imported from legacy browser trash",
                )
        known_ids = set(imported["by_id"])
        report = {
            "interested_ids": sorted(legacy["interested"]),
            "trashed_ids": sorted(legacy["trashed"]),
            "unknown_interested_ids": sorted(legacy["interested"] - known_ids),
            "unknown_trashed_ids": sorted(legacy["trashed"] - known_ids),
        }
        return report

    def _write_report(self, report, path):
        if path:
            path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        unknown = report["unknown_interested_ids"] + report["unknown_trashed_ids"]
        self.stdout.write(
            "legacy state: "
            f"interested={len(report['interested_ids'])}, trashed={len(report['trashed_ids'])}, "
            f"unknown={len(unknown)}"
        )
        if unknown:
            self.stdout.write(json.dumps({"unknown_interested_ids": report["unknown_interested_ids"], "unknown_trashed_ids": report["unknown_trashed_ids"]}))
