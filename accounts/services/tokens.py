import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone


TOKEN_BYTES = 32


def digest_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_agent_token(*, user, name, scopes, project_ids=None, expires_at=None):
    """Create a token and return ``(model, raw_token)`` once; raw_token is never stored."""
    from accounts.models import AgentToken

    raw_token = secrets.token_urlsafe(TOKEN_BYTES)
    token = AgentToken.objects.create(
        user=user,
        name=name,
        token_prefix=raw_token[:12],
        digest=digest_token(raw_token),
        scopes=sorted(set(scopes)),
        project_ids=[str(value) for value in (project_ids or [])],
        expires_at=expires_at or timezone.now() + timedelta(days=settings.AGENT_TOKEN_DEFAULT_DAYS),
    )
    return token, raw_token


def revoke_agent_token(token):
    if token.revoked_at is None:
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
