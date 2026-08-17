"""Database-backed throttling for password-bearing and agent-write endpoints."""

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

# Pairing starts, per client IP.  Links live 10 minutes, so this doubles as the
# cap on concurrent pending links from one address without storing the address.
PAIRING_WINDOW = timedelta(minutes=15)
PAIRING_MAX = 10

# Agent write budget, per token.  §6.3 caps a run at 120 writes and 0 destroys;
# these are the server-side backstops behind that convention.
MUTATION_WINDOW = timedelta(hours=1)
MUTATION_MAX = 500
DESTROY_WINDOW = timedelta(days=1)
DESTROY_MAX = 50


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


@transaction.atomic
def _consume_counters(specs, amount=1):
    """Charge ``amount`` against every ``(kind, value, limit, window)`` bucket.

    All-or-nothing: if any bucket is already at its limit nothing is charged and
    the caller is told how long the fullest window has left.  Reuses the
    ``AuthThrottle`` rows — the same hashed-bucket shape, a different ``kind``,
    so auth throttling is untouched.
    """
    now = timezone.now()
    buckets = []
    for kind, value, limit, window in specs:
        bucket, _ = AuthThrottle.objects.select_for_update().get_or_create(
            key_digest=_digest(kind, value),
            defaults={"window_started_at": now},
        )
        if now - bucket.window_started_at >= window:
            bucket.failure_count = 0
            bucket.window_started_at = now
            bucket.blocked_until = None
        buckets.append((bucket, limit, window))
    fields = ["failure_count", "window_started_at", "blocked_until", "updated_at"]
    over = [(b, w) for b, limit, w in buckets if b.failure_count + amount > limit]
    if over:
        retry_after = 1
        for bucket, window in over:
            bucket.blocked_until = bucket.window_started_at + window
            retry_after = max(
                retry_after, int((bucket.blocked_until - now).total_seconds()) + 1
            )
        for bucket, _limit, _window in buckets:
            bucket.save(update_fields=fields)
        return True, retry_after
    for bucket, _limit, _window in buckets:
        bucket.failure_count += amount
        bucket.save(update_fields=fields)
    return False, 0


def consume_pairing(request):
    """Rate-limit device-code creation by client IP; ``(blocked, retry_after)``."""
    remote_addr = request.META.get("REMOTE_ADDR", "") or "unknown"
    return _consume_counters(
        [("pairing-ip", remote_addr, PAIRING_MAX, PAIRING_WINDOW)]
    )


def consume_mutation(token_id, *, amount=1, destructive=False):
    """Charge an agent write against its token's budget; ``(blocked, retry_after)``.

    Session principals pass ``token_id=None`` and are never throttled here: a
    human at a keyboard is already rate-limited by being human.
    """
    if not token_id:
        return False, 0
    specs = [("mutation-write", str(token_id), MUTATION_MAX, MUTATION_WINDOW)]
    if destructive:
        specs.append(("mutation-destroy", str(token_id), DESTROY_MAX, DESTROY_WINDOW))
    return _consume_counters(specs, amount=max(1, int(amount)))
