import hashlib
import secrets
import uuid
from datetime import timedelta

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.core.validators import MaxLengthValidator
from django.db import models
from django.utils import timezone

from .managers import UserManager, normalize_email


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True, max_length=254)
    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    date_joined = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        ordering = ("email",)

    def save(self, *args, **kwargs):
        self.email = normalize_email(self.email)
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.email


class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    display_name = models.CharField(max_length=120)
    timezone = models.CharField(max_length=64, default="UTC")
    bio = models.TextField(blank=True, validators=[MaxLengthValidator(5000)])
    personal_details = models.JSONField(default=dict, blank=True)
    # Pausing has to live on the server: the user cannot reliably stop a
    # scheduled job that an agent installed on their behalf.
    agent_paused_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.display_name or self.user.email


class SavedPrompt(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="saved_prompts")
    title = models.CharField(max_length=200)
    prompt = models.TextField(validators=[MaxLengthValidator(30000)])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at",)
        constraints = [
            models.UniqueConstraint(fields=("user", "title"), name="accounts_savedprompt_user_title_uniq"),
        ]

    def __str__(self):
        return f"{self.user.email}: {self.title}"


class AgentToken(models.Model):
    """A revocable bearer token; only the SHA-256 digest is persisted."""

    id = models.UUIDField(primary_key=True, editable=False, default=uuid.uuid4)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="agent_tokens")
    name = models.CharField(max_length=120)
    token_prefix = models.CharField(max_length=16, editable=False)
    digest = models.CharField(max_length=64, unique=True, editable=False)
    scopes = models.JSONField(default=list)
    project_ids = models.JSONField(default=list, blank=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    # How often the agent said it intends to run, and where it runs.  Both are
    # descriptive only; nothing authorizes off them.
    expected_cadence_minutes = models.PositiveIntegerField(null=True, blank=True)
    environment_note = models.CharField(max_length=200, blank=True)
    # True when the raw token was shown in a chat/clipboard flow.  Such a token
    # is compromised at birth and the UI says so.
    exposed_to_chat = models.BooleanField(default=False)

    class Meta:
        ordering = ("-created_at",)

    @property
    def is_valid(self):
        return self.revoked_at is None and self.expires_at > timezone.now() and self.user.is_active

    def __str__(self):
        return f"{self.name} ({self.token_prefix})"


# Crockford base32 minus I, L, O and U: no character pair a person can confuse
# when reading a code off one screen and typing it into another.
USER_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
USER_CODE_LENGTH = 6
USER_CODE_FIXUPS = {"I": "1", "L": "1", "O": "0"}
AGENT_LINK_TTL = timedelta(minutes=10)
AGENT_LINK_INTERVAL_SECONDS = 5
# A well-behaved client polls every 5s for at most 10 minutes (~120 polls).
AGENT_LINK_MAX_POLLS = 400


def new_user_code():
    return "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(USER_CODE_LENGTH))


def normalize_user_code(value):
    """Fold the characters the alphabet deliberately excludes back onto their twins."""
    folded = "".join(USER_CODE_FIXUPS.get(ch, ch) for ch in str(value or "").upper())
    return "".join(ch for ch in folded if ch in USER_CODE_ALPHABET)[:USER_CODE_LENGTH]


def device_code_digest(raw):
    return hashlib.sha256(str(raw).encode("utf-8")).hexdigest()


class AgentLink(models.Model):
    """One device-code pairing attempt.

    The ``device_code`` is the agent's half and is never stored in the clear;
    the six-character ``user_code`` is the human-visible half that makes the
    approval card phishing-resistant.  Neither half is ever written to an
    audit summary.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        DENIED = "denied", "Denied"
        EXPIRED = "expired", "Expired"
        CONSUMED = "consumed", "Consumed"

    id = models.UUIDField(primary_key=True, editable=False, default=uuid.uuid4)
    device_code_hash = models.CharField(max_length=64, unique=True, editable=False)
    user_code = models.CharField(max_length=8, db_index=True)
    agent_label = models.CharField(max_length=120)
    environment_note = models.CharField(max_length=200, blank=True)
    requested_cadence_minutes = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    interval_seconds = models.PositiveIntegerField(default=AGENT_LINK_INTERVAL_SECONDS)
    poll_count = models.PositiveIntegerField(default=0)
    last_polled_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="approved_agent_links"
    )
    issued_token = models.ForeignKey(
        AgentToken, null=True, blank=True, on_delete=models.SET_NULL, related_name="agent_links"
    )

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("status", "expires_at"), name="accounts_agentlink_status_idx")
        ]

    def __str__(self):
        return f"{self.agent_label} ({self.user_code})"

    @property
    def is_expired(self):
        return self.expires_at <= timezone.now() or self.poll_count > AGENT_LINK_MAX_POLLS

    @property
    def is_open(self):
        """Still awaiting a human decision."""
        return self.status == self.Status.PENDING and not self.is_expired

    @classmethod
    def open_for_code(cls, user_code):
        """Return the one live pending link for a typed code, or ``None``."""
        code = normalize_user_code(user_code)
        if len(code) != USER_CODE_LENGTH:
            return None
        return (
            cls.objects.filter(
                user_code=code,
                status=cls.Status.PENDING,
                expires_at__gt=timezone.now(),
            )
            .order_by("-created_at")
            .first()
        )

    def audit(self, action, actor=None, extra=None, request_id=""):
        """Write one bounded audit row.  Never carries the device code or token."""
        from projects.services.mutations import append_audit

        summary = {"agent_label": self.agent_label, "status": self.status}
        if self.environment_note:
            summary["environment_note"] = self.environment_note
        summary.update(extra or {})
        return append_audit(
            None, f"agent_link.{action}", "agent_link", str(self.pk), summary, actor,
            request_id=request_id,
        )

    def approve(self, user, *, request_id=""):
        self.status = self.Status.APPROVED
        self.approved_by = user
        self.save(update_fields=["status", "approved_by"])
        self.audit("approved", user, request_id=request_id)
        return self

    def deny(self, user, *, request_id=""):
        self.status = self.Status.DENIED
        self.approved_by = user
        self.save(update_fields=["status", "approved_by"])
        self.audit("denied", user, request_id=request_id)
        return self

    def expire(self, *, request_id=""):
        self.status = self.Status.EXPIRED
        self.save(update_fields=["status"])
        self.audit("expired", None, request_id=request_id)
        return self


class AuthThrottle(models.Model):
    """Hashed identity bucket used to rate-limit password endpoints.

    ``key_digest`` is an HMAC-derived value; neither email addresses nor
    client IP addresses are retained in the database.
    """

    key_digest = models.CharField(max_length=64, unique=True, editable=False)
    failure_count = models.PositiveIntegerField(default=0)
    window_started_at = models.DateTimeField()
    blocked_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=("blocked_until",))]

    def __str__(self):
        return self.key_digest[:12]
