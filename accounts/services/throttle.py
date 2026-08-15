"""Database-backed throttling for password-bearing API endpoints."""

import hashlib
import hmac
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from accounts.managers import normalize_email
from accounts.models import AuthThrottle


WINDOW = timedelta(minutes=15)
BLOCK = timedelta(minutes=15)
MAX_FAILURES = 5


def _digest(kind, value):
    # HMAC prevents offline dictionary attacks against low-entropy IP values
    # while keeping the bucket deterministic across workers/processes.
    message = f"auth-throttle:{kind}:{value}".encode("utf-8")
    return hmac.new(str(settings.SECRET_KEY).encode("utf-8"), message, hashlib.sha256).hexdigest()


def request_keys(request, email=""):
    """Return deterministic, non-sensitive IP/email bucket identifiers."""
    remote_addr = request.META.get("REMOTE_ADDR", "") or "unknown"
    keys = [_digest("ip", remote_addr)]
    normalized = normalize_email(email)
    if normalized:
        keys.append(_digest("email", normalized))
    return tuple(dict.fromkeys(keys))


@transaction.atomic
def consume(keys):
    """Record one failed attempt and return ``(blocked, retry_after)``.

    Rows are locked before checking/incrementing, so concurrent Gunicorn
    workers cannot bypass the threshold.  A request blocked by either its IP
    or email bucket receives the same generic response.
    """
    now = timezone.now()
    retry_after = 0
    blocked = False
    for key in keys:
        bucket, _ = AuthThrottle.objects.select_for_update().get_or_create(
            key_digest=key,
            defaults={"window_started_at": now},
        )
        if bucket.blocked_until and bucket.blocked_until > now:
            blocked = True
            retry_after = max(retry_after, int((bucket.blocked_until - now).total_seconds()) + 1)
            continue
        if now - bucket.window_started_at >= WINDOW:
            bucket.failure_count = 0
            bucket.window_started_at = now
        bucket.failure_count += 1
        if bucket.failure_count > MAX_FAILURES:
            bucket.blocked_until = now + BLOCK
            blocked = True
            retry_after = max(retry_after, int(BLOCK.total_seconds()))
        bucket.save(update_fields=["failure_count", "window_started_at", "blocked_until", "updated_at"])
    return blocked, max(retry_after, 1) if blocked else 0


@transaction.atomic
def reset(keys):
    """Reset buckets after a successful authentication/registration."""
    AuthThrottle.objects.select_for_update().filter(key_digest__in=keys).update(
        failure_count=0,
        blocked_until=None,
        window_started_at=timezone.now(),
    )
