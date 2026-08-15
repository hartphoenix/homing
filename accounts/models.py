import uuid

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
    display_name = models.CharField(max_length=120, blank=True)
    timezone = models.CharField(max_length=64, default="UTC")
    bio = models.TextField(blank=True, validators=[MaxLengthValidator(5000)])
    personal_details = models.JSONField(default=dict, blank=True)
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

    class Meta:
        ordering = ("-created_at",)

    @property
    def is_valid(self):
        return self.revoked_at is None and self.expires_at > timezone.now() and self.user.is_active

    def __str__(self):
        return f"{self.name} ({self.token_prefix})"


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
