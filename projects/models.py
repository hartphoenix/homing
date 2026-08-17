import hashlib
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from accounts.managers import normalize_email


def normalize_listing_url(value):
    """Conservatively canonicalize a URL for fallback identity only."""
    if not value:
        return ""
    parts = urlsplit(value.strip())
    query = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True) if not key.lower().startswith(("utm_", "fbclid"))]
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def listing_identity_hash(url):
    return hashlib.sha256(normalize_listing_url(url).encode("utf-8")).hexdigest() if url else ""


class Project(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        TRASHED = "trashed", "Trashed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True)
    description = models.TextField(blank=True, validators=[MaxLengthValidator(10000)])
    prompt = models.TextField(blank=True, validators=[MaxLengthValidator(30000)])
    criteria = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_projects")
    prompt_revision = models.PositiveIntegerField(default=0)
    latest_change_sequence = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at",)

    def __str__(self):
        return self.name


class ProjectMembership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        EDITOR = "editor", "Editor"
        VIEWER = "viewer", "Viewer"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="project_memberships")
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.VIEWER)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("project", "user"), name="projects_membership_project_user_uniq")]
        ordering = ("project", "role", "joined_at")

    def __str__(self):
        return f"{self.user} · {self.project} · {self.role}"


class ProjectInvitation(models.Model):
    class Role(models.TextChoices):
        EDITOR = "editor", "Editor"
        VIEWER = "viewer", "Viewer"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="invitations")
    invited_email = models.EmailField(max_length=254)
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.VIEWER)
    inviter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sent_invitations")
    token_digest = models.CharField(max_length=64, unique=True, editable=False)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("project", "invited_email", "created_at"))]

    def save(self, *args, **kwargs):
        self.invited_email = normalize_email(self.invited_email)
        super().save(*args, **kwargs)

    @property
    def is_pending(self):
        return not self.accepted_at and not self.revoked_at and self.expires_at > timezone.now()


class PromptRevision(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="prompt_revisions")
    revision = models.PositiveIntegerField()
    prompt = models.TextField(validators=[MaxLengthValidator(30000)])
    criteria = models.JSONField(default=dict, blank=True)
    editor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="prompt_revisions")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-project", "-revision")
        constraints = [models.UniqueConstraint(fields=("project", "revision"), name="projects_promptrevision_project_revision_uniq")]


class SearchRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        CLAIMED = "claimed", "Claimed"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="search_runs")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="search_runs")
    agent_token = models.ForeignKey("accounts.AgentToken", null=True, blank=True, on_delete=models.SET_NULL, related_name="search_runs")
    agent_label = models.CharField(max_length=160, blank=True)
    prompt_revision = models.PositiveIntegerField(default=0)
    prompt_snapshot = models.TextField(validators=[MaxLengthValidator(30000)])
    criteria_snapshot = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    lease_owner = models.CharField(max_length=160, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    claim_token = models.CharField(max_length=64, blank=True, editable=False)
    attempt_count = models.PositiveIntegerField(default=0)
    input_cursor = models.CharField(max_length=500, blank=True)
    output_cursor = models.CharField(max_length=500, blank=True)
    continuation = models.JSONField(default=dict, blank=True)
    result_counts = models.JSONField(default=dict, blank=True)
    summary = models.TextField(blank=True, validators=[MaxLengthValidator(10000)])
    idempotency_key = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=("project", "user", "idempotency_key"), name="projects_searchrun_project_user_idem_uniq", condition=~models.Q(idempotency_key="")),
        ]
        indexes = [models.Index(fields=("project", "status", "created_at"))]


class Lead(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        TRASHED = "trashed", "Trashed"

    class HousingType(models.TextChoices):
        ENTIRE = "entire", "Entire place"
        SHARED = "shared", "Shared"
        UNKNOWN = "unknown", "Unknown"

    class DateConfidence(models.TextChoices):
        STRONG = "strong", "Strong"
        VERIFY = "verify", "Verify"
        UNKNOWN = "unknown", "Unknown"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="leads")
    source = models.CharField(max_length=160)
    source_listing_id = models.CharField(max_length=200, blank=True)
    canonical_url = models.URLField(max_length=2000)
    source_url = models.URLField(max_length=2000, blank=True)
    identity_hash = models.CharField(max_length=64, blank=True, editable=False)
    title = models.CharField(max_length=500)
    summary = models.TextField(blank=True, validators=[MaxLengthValidator(30000)])
    location = models.CharField(max_length=500, blank=True)
    price_display = models.CharField(max_length=120, blank=True)
    price_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    price_currency = models.CharField(max_length=3, default="USD")
    availability = models.CharField(max_length=500, blank=True)
    housing_type = models.CharField(max_length=16, choices=HousingType.choices, default=HousingType.UNKNOWN)
    date_confidence = models.CharField(max_length=16, choices=DateConfidence.choices, default=DateConfidence.UNKNOWN)
    park_notes = models.CharField(max_length=1000, blank=True)
    attributes = models.JSONField(default=dict, blank=True)
    verification_notes = models.TextField(blank=True, validators=[MaxLengthValidator(10000)])
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    trashed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="trashed_leads")
    trashed_at = models.DateTimeField(null=True, blank=True)
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_leads")
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at",)
        constraints = [
            models.UniqueConstraint(fields=("project", "source", "source_listing_id"), name="projects_lead_source_identity_uniq", condition=~models.Q(source_listing_id="")),
            models.UniqueConstraint(fields=("project", "identity_hash"), name="projects_lead_url_identity_uniq", condition=~models.Q(identity_hash="")),
        ]
        indexes = [models.Index(fields=("project", "status")), models.Index(fields=("project", "date_confidence"))]

    def clean(self):
        super().clean()
        for field in ("canonical_url", "source_url"):
            value = getattr(self, field)
            if value and urlsplit(value).scheme not in {"http", "https"}:
                raise ValidationError({field: "Only HTTP(S) URLs are accepted."})

    def save(self, *args, **kwargs):
        self.identity_hash = listing_identity_hash(self.canonical_url)
        self.full_clean()
        return super().save(*args, **kwargs)

    @property
    def etag(self):
        return f'"{self.revision}"'


class LeadInterest(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="interests")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="lead_interests")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("lead", "user"), name="projects_leadinterest_lead_user_uniq")]


class LeadComment(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="lead_comments")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="replies")
    body = models.TextField(validators=[MaxLengthValidator(10000)])
    created_at = models.DateTimeField(auto_now_add=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("created_at",)


class ProjectChange(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="changes")
    sequence = models.PositiveBigIntegerField()
    event_type = models.CharField(max_length=80)
    object_type = models.CharField(max_length=80)
    object_id = models.CharField(max_length=100, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    tombstone = models.BooleanField(default=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="project_changes")
    actor_kind = models.CharField(max_length=24, default="user")
    token_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("project", "sequence")
        constraints = [models.UniqueConstraint(fields=("project", "sequence"), name="projects_change_project_sequence_uniq")]
        indexes = [models.Index(fields=("project", "created_at"))]


class AuditEvent(models.Model):
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.CASCADE, related_name="audit_events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_events")
    actor_kind = models.CharField(max_length=24, default="user")
    token_id = models.UUIDField(null=True, blank=True)
    request_id = models.CharField(max_length=100, blank=True)
    action = models.CharField(max_length=100)
    object_type = models.CharField(max_length=80, blank=True)
    object_id = models.CharField(max_length=100, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("project", "created_at"))]


class IdempotencyKey(models.Model):
    """Bounded replay record for write endpoints; response is intentionally JSON."""
    token = models.ForeignKey("accounts.AgentToken", null=True, blank=True, on_delete=models.CASCADE, related_name="idempotency_keys")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="idempotency_keys")
    endpoint = models.CharField(max_length=200)
    key = models.CharField(max_length=200)
    request_hash = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=("token", "endpoint", "key"), name="projects_idempotency_token_endpoint_key_uniq")]
        indexes = [models.Index(fields=("expires_at",))]
