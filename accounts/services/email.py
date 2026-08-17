"""Transactional email helpers used by the browser and API flows.

The application deliberately uses Django's standard mail interface.  Local
development defaults to the console backend, while a deployment can select
SMTP (Resend, Gmail, or another provider) entirely through environment
variables.  No provider-specific SDK or credentials are required here.
"""

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse


def invitation_url(raw_token, request=None):
    """Return the browser invitation URL, preferring the configured public URL."""

    path = reverse("tracker:invitation-accept", args=[raw_token])
    public_base = getattr(settings, "PUBLIC_BASE_URL", "").rstrip("/")
    if public_base:
        return f"{public_base}{path}"
    if request is not None:
        return request.build_absolute_uri(path)
    return path


def send_invitation_email(*, invitation, raw_token, request=None):
    """Send a project invitation using Django's configured backend."""

    url = invitation_url(raw_token, request=request)
    inviter_profile = getattr(invitation.inviter, "profile", None)
    inviter_name = (
        (inviter_profile.display_name if inviter_profile else "")
        or invitation.inviter.email
    )
    send_mail(
        subject=f"You are invited to join {invitation.project.name} on Homing",
        message=(
            f"{inviter_name} invited you to collaborate on {invitation.project.name}.\n\n"
            f"Open this link to accept the invitation:\n{url}\n\n"
            "This link expires in 7 days and can only be used once."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[invitation.invited_email],
        fail_silently=False,
    )
    return url
