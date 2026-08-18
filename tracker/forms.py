"""Small, server-rendered forms for the tracker web interface.

The API has its own validation layer.  These forms deliberately keep JSON fields
as textareas so an agent's structured values are not silently flattened when a
human edits a record.
"""

import json

from django import forms
from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone

from accounts.managers import normalize_email
from accounts.models import Profile, SavedPrompt, User
from projects.models import (
    Lead,
    LeadComment,
    Project,
)


class JSONTextareaMixin:
    json_fields = ()

    def clean(self):
        cleaned = super().clean()
        for name in self.json_fields:
            raw = cleaned.get(name)
            if raw in (None, ""):
                cleaned[name] = {}
                continue
            if isinstance(raw, (dict, list)):
                continue
            try:
                cleaned[name] = json.loads(raw)
            except (TypeError, ValueError):
                self.add_error(name, 'Enter valid JSON (for example, {"key": "value"}).')
            else:
                if not isinstance(cleaned[name], dict):
                    self.add_error(name, "Enter a JSON object, not a list or scalar value.")
        return cleaned


class RegisterForm(forms.ModelForm):
    display_name = forms.CharField(
        label="Nickname",
        max_length=120,
        help_text="This is the name other project members will see.",
    )
    password1 = forms.CharField(
        label="Password",
        widget=forms.PasswordInput,
        min_length=12,
        help_text="Use at least 12 characters. Avoid common passwords.",
    )
    password2 = forms.CharField(label="Confirm password", widget=forms.PasswordInput)

    class Meta:
        model = User
        fields = ("email",)

    def __init__(self, *args, locked_email=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.locked_email = normalize_email(locked_email) if locked_email else ""
        if self.locked_email:
            self.initial["email"] = self.locked_email
            self.fields["email"].widget.attrs["readonly"] = True

    def clean_email(self):
        email = normalize_email(self.cleaned_data["email"])
        if self.locked_email and email != self.locked_email:
            raise ValidationError("Use the invited email address to create this account.")
        if User.objects.filter(email=email).exists():
            raise ValidationError("Unable to register with these details.")
        return email

    def clean_display_name(self):
        display_name = self.cleaned_data["display_name"].strip()
        if not display_name:
            raise ValidationError("Enter a nickname.")
        return display_name

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password1")
        if password:
            # Keep password policy in Django's configured validators so a
            # deployment can add organization-specific checks without
            # changing this form. The 12-character floor is enforced by the
            # field above even when no validators are configured.
            if cleaned.get("email"):
                self.instance.email = cleaned["email"]
            try:
                validate_password(password, self.instance)
            except ValidationError as error:
                self.add_error("password1", error)
        if cleaned.get("password1") and cleaned.get("password1") != cleaned.get("password2"):
            self.add_error("password2", "The passwords do not match.")
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = normalize_email(self.cleaned_data["email"])
        user.set_password(self.cleaned_data["password1"])
        if commit:
            user.save()
            Profile.objects.update_or_create(
                user=user,
                defaults={"display_name": self.cleaned_data["display_name"].strip()},
            )
        return user


class EmailAuthenticationForm(forms.Form):
    email = forms.EmailField()
    password = forms.CharField(widget=forms.PasswordInput)

    def __init__(self, request=None, *args, **kwargs):
        self.request = request
        self.user_cache = None
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("email") and cleaned.get("password"):
            self.user_cache = authenticate(
                self.request,
                username=normalize_email(cleaned["email"]),
                password=cleaned["password"],
            )
            if self.user_cache is None:
                raise ValidationError("Email or password is incorrect.")
            if not self.user_cache.is_active:
                raise ValidationError("This account is inactive.")
        return cleaned

    def get_user(self):
        return self.user_cache


class ProfileForm(JSONTextareaMixin, forms.ModelForm):
    personal_details = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text="Optional JSON for private context (never shared with project members).",
    )
    json_fields = ("personal_details",)

    class Meta:
        model = Profile
        fields = ("display_name", "timezone", "bio", "personal_details")
        widgets = {"bio": forms.Textarea(attrs={"rows": 5})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.initial["personal_details"] = json.dumps(
                self.instance.personal_details or {}, indent=2
            )


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ("name", "description")
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}

    def __init__(self, *args, creator_email=None, **kwargs):
        editing = kwargs.get("instance") is not None
        # Keep the voice-memo wording ("initial prompt") accepted by older
        # callers while exposing the model's natural ``prompt`` field name.
        if not editing and (args or kwargs.get("data") is not None):
            data = args[0] if args else kwargs.get("data")
            if (
                hasattr(data, "copy")
                and not data.get("prompt")
                and data.get("initial_prompt")
            ):
                data = data.copy()
                data["prompt"] = data.get("initial_prompt", "")
                if args:
                    args = (data, *args[1:])
                else:
                    kwargs["data"] = data
        super().__init__(*args, **kwargs)
        self.creator_email = normalize_email(creator_email)
        # Initial prompt and invitations belong to creation, not metadata
        # editing. They remain available as fields on the bound create form.
        if not editing:
            self.fields["prompt"] = forms.CharField(
                required=False,
                label="Initial search prompt (optional)",
                widget=forms.Textarea(attrs={"rows": 8}),
                help_text="Tell the agent what to search for. You can add or change this later.",
            )
            self.fields["invite_emails"] = forms.CharField(
                required=False,
                label="Invite collaborators (optional)",
                widget=forms.Textarea(
                    attrs={
                        "rows": 4,
                        "placeholder": "one@example.com, another@example.com",
                    }
                ),
                help_text="Enter multiple email addresses separated by commas, spaces, or new lines.",
            )

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise ValidationError("Enter a project name.")
        return name

    def clean_prompt(self):
        return self.cleaned_data.get("prompt", "").strip()

    def clean_invite_emails(self):
        raw = self.cleaned_data.get("invite_emails", "")
        emails = []
        seen = set()
        for token in raw.replace(",", " ").replace(";", " ").split():
            try:
                email = normalize_email(validate_email(token) or token)
            except ValidationError:
                raise ValidationError(f"Enter a valid email address: {token}")
            # The creator already belongs to the project; quietly omit their
            # address while retaining any other valid invitees.
            if email == self.creator_email:
                continue
            if email not in seen:
                seen.add(email)
                emails.append(email)
        return emails


class PromptForm(JSONTextareaMixin, forms.Form):
    prompt = forms.CharField(widget=forms.Textarea(attrs={"rows": 12}), required=False)
    criteria = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 8}),
        help_text="Optional JSON criteria for agents.",
    )
    json_fields = ("criteria",)

    def __init__(self, *args, project=None, **kwargs):
        self.project = project
        super().__init__(*args, **kwargs)
        if not self.is_bound and project is not None:
            self.initial.setdefault("prompt", project.prompt)
            self.initial.setdefault("criteria", json.dumps(project.criteria or {}, indent=2))


class LeadForm(JSONTextareaMixin, forms.ModelForm):
    expected_revision = forms.IntegerField(required=False, widget=forms.HiddenInput)
    attributes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text="Optional JSON attributes.",
    )
    json_fields = ("attributes",)
    canonical_url = forms.URLField(max_length=2000, assume_scheme="https")
    source_url = forms.URLField(max_length=2000, required=False, assume_scheme="https")

    class Meta:
        model = Lead
        fields = (
            "source",
            "source_listing_id",
            "canonical_url",
            "source_url",
            "title",
            "summary",
            "location",
            "price_display",
            "price_amount",
            "price_currency",
            "availability",
            "housing_type",
            "date_confidence",
            "park_notes",
            "attributes",
            "verification_notes",
            "expected_revision",
        )
        widgets = {
            "summary": forms.Textarea(attrs={"rows": 5}),
            "verification_notes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["price_currency"].required = False
        if self.instance and self.instance.pk and not self.is_bound:
            self.initial["attributes"] = json.dumps(self.instance.attributes or {}, indent=2)
            self.initial["expected_revision"] = self.instance.revision


class TrashLeadForm(forms.Form):
    comment = forms.CharField(
        max_length=10000,
        required=False,
        label="Comment (optional)",
        widget=forms.Textarea(attrs={"rows": 4, "maxlength": 10000}),
        help_text="Optionally add context. A blank comment is fine.",
    )

    def __init__(self, *args, **kwargs):
        # Keep old callers that supplied the former ``reason`` initial value
        # working while the web UI moves to normal chronological comments.
        initial = kwargs.get("initial") or {}
        if "comment" not in initial and "reason" in initial:
            initial = dict(initial)
            initial["comment"] = initial["reason"]
            kwargs["initial"] = initial
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        # Older clients may still submit ``reason``. Treat it as a comment so
        # the transition does not silently discard an existing note.
        if not cleaned.get("comment") and self.data.get("reason"):
            cleaned["comment"] = self.data.get("reason", "").strip()
        return cleaned


class CommentForm(forms.ModelForm):
    class Meta:
        model = LeadComment
        fields = ("body",)
        widgets = {"body": forms.Textarea(attrs={"rows": 6, "maxlength": 10000})}


class InvitationForm(forms.Form):
    email = forms.EmailField(label="Member email")

    def clean_email(self):
        return normalize_email(self.cleaned_data["email"])


class AgentTokenForm(forms.Form):
    name = forms.CharField(
        max_length=120,
        initial="My search agent",
        help_text="A private label to help you recognize this connection later.",
    )
    expires_at = forms.DateTimeField(
        required=False,
        label="Expiry (optional)",
        help_text="Leave blank to use the site's default. You can revoke access at any time.",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )

    def clean_expires_at(self):
        value = self.cleaned_data.get("expires_at")
        if value and timezone.is_naive(value):
            value = timezone.make_aware(value, timezone.get_current_timezone())
        if value and value <= timezone.now():
            raise ValidationError("Choose a future expiry time.")
        return value


class SavedPromptForm(forms.ModelForm):
    class Meta:
        model = SavedPrompt
        fields = ("title", "prompt")
        widgets = {"prompt": forms.Textarea(attrs={"rows": 12})}
