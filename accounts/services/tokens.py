import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone


TOKEN_BYTES = 32


def digest_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def default_agent_scopes():
    """Every scope an agent may hold: the full set minus the destructive one.

    ``leads:destroy`` is a human verb.  Withholding it turns the client-side
    "the agent never trashes or restores" convention into a server-side control.
    """
    from projects.services.authorization import SCOPES

    return frozenset(SCOPES) - {"leads:destroy"}


def create_agent_token(
    *,
    user,
    name,
    scopes,
    project_ids=None,
    expires_at=None,
    environment_note="",
    expected_cadence_minutes=None,
    exposed_to_chat=False,
):
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
        environment_note=str(environment_note or "")[:200],
        expected_cadence_minutes=expected_cadence_minutes,
        exposed_to_chat=bool(exposed_to_chat),
    )
    return token, raw_token


def create_paired_agent_token(
    *, user, name, environment_note="", expected_cadence_minutes=None, expires_at=None
):
    """Mint the token issued by device-code pairing.

    The raw value goes straight from the response into the agent's secret
    store, so ``exposed_to_chat`` is False and the scope set is non-destructive.
    """
    return create_agent_token(
        user=user,
        name=name,
        scopes=default_agent_scopes(),
        expires_at=expires_at,
        environment_note=environment_note,
        expected_cadence_minutes=expected_cadence_minutes,
        exposed_to_chat=False,
    )


def revoke_agent_token(token):
    if token.revoked_at is None:
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
