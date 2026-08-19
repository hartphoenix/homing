"""Small, dependency-free JSON API.  All authorization is delegated to the project policy."""

import base64
import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.http import JsonResponse, Http404
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.middleware.csrf import CsrfViewMiddleware
from django.utils.text import slugify
from django.db import IntegrityError, transaction
from django.db.models import Q

from accounts.models import (
    AGENT_LINK_INTERVAL_SECONDS,
    AGENT_LINK_TTL,
    AgentLink,
    AgentToken,
    Profile,
    SavedPrompt,
    User,
    device_code_digest,
    new_user_code,
)
from accounts.managers import normalize_email
from accounts.services.email import send_invitation_email
from accounts.services.tokens import (
    create_agent_token,
    create_paired_agent_token,
    digest_token,
    revoke_agent_token,
)
from accounts.services.throttle import (
    consume as consume_auth_attempt,
    consume_mutation,
    consume_pairing,
    request_keys,
    reset as reset_auth_attempts,
)
from projects.models import (
    Lead,
    LeadComment,
    Project,
    IdempotencyKey,
    listing_identity_hash,
    ProjectInvitation,
    ProjectMembership,
    PromptRevision,
    SearchRun,
)
from projects.services.authorization import (
    Principal,
    SCOPES,
    assert_owner,
    authorize_project,
    project_is_restricted,
)
from projects.services.mutations import (
    LeadRevisionConflict,
    FinalOwnerError,
    PromptRevisionConflict,
    append_change,
    append_audit,
    batch_lead_mutation,
    restore_lead,
    remove_project_member,
    set_interest,
    trash_lead,
    update_project_prompt,
)


def _request_id(request):
    value = request.headers.get("X-Request-ID", "")[:100]
    return value or f"req_{uuid.uuid4().hex}"


def _response(request, data=None, status=200, headers=None):
    response = JsonResponse(
        data if data is not None else {}, status=status, safe=isinstance(data, dict)
    )
    response["X-Request-ID"] = _request_id(request)
    for key, value in (headers or {}).items():
        response[key] = value
    return response


def _error(request, code, message, status, fields=None, headers=None):
    return _response(
        request,
        {
            "error": {
                "code": code,
                "message": message,
                "fields": fields or {},
                "request_id": _request_id(request),
            }
        },
        status,
        headers,
    )


def _rate_limited(request, retry_after, message="Too many authentication attempts. Try again later."):
    return _error(
        request,
        "rate_limited",
        message,
        429,
        headers={"Retry-After": str(max(1, retry_after))},
    )


def _public_origin(request):
    """The origin agents were told to use, with no trailing slash."""
    public_base = getattr(settings, "PUBLIC_BASE_URL", "")
    return public_base.rstrip("/") if public_base else request.build_absolute_uri("/").rstrip("/")


def _unauthorized(request):
    """401 with the pointer an agent needs to re-pair after its token dies."""
    challenge = (
        'Bearer realm="homing", error="invalid_token", '
        f'resource_metadata="{_public_origin(request)}/agent/"'
    )
    return _error(
        request,
        "unauthorized",
        "Authentication is required.",
        401,
        headers={"WWW-Authenticate": challenge},
    )


def _body(request):
    if request.body and len(request.body) > 2_000_000:
        raise ValueError("request body is too large")
    try:
        value = json.loads(request.body or b"{}")
    except (TypeError, json.JSONDecodeError):
        raise ValueError("Body must be valid JSON")
    if not isinstance(value, dict):
        raise ValueError("Body must be a JSON object")
    return value


def _data(request):
    try:
        return _body(request), None
    except ValueError as exc:
        return None, _error(request, "invalid_json", str(exc), 422)


class _SessionCsrf(CsrfViewMiddleware):
    """Enforce CSRF for session-authenticated API calls only."""

    def _reject(self, request, reason):
        return reason


def _session_csrf_failure(request):
    """Return a rejection reason when a cookie-authenticated unsafe request
    lacks a valid CSRF token, else None.

    The whole /api/v1 surface is exempt from the blanket CSRF middleware
    because a bearer credential is not ambient authority: it is never attached
    by the browser, so it cannot be replayed cross-site and CSRF adds nothing.
    A session cookie IS ambient, so a cookie-authenticated unsafe request is
    still checked here, exactly as the middleware would have.
    """
    if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
        return None
    if request.headers.get("Authorization", ""):
        return None
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    return _SessionCsrf(lambda req: None).process_view(request, None, (), {})


def _principal(request):
    # A bearer credential is an explicit principal.  Never silently fall back
    # to a browser session when an Authorization header is present: otherwise
    # a browser carrying a session cookie could accidentally grant a scoped
    # agent request the session user's full authority.
    authorization = request.headers.get("Authorization", "")
    if authorization:
        if not authorization.startswith("Bearer "):
            return None
        raw = authorization[7:].strip()
        if not raw or len(raw) > 256:
            return None
        token = AgentToken.objects.select_related("user").filter(digest=digest_token(raw)).first()
        if not token or not token.is_valid:
            return None
        token.last_used_at = timezone.now()
        token.save(update_fields=["last_used_at"])
        return Principal(token.user, token)
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return Principal(user)
    return None


def _human_only(principal):
    """Reject account/project administration through scoped agent tokens."""
    if principal.token:
        raise PermissionDenied("This operation requires an authenticated session.")


def _project_ids(value):
    """Validate the JSON shape used for optional token restrictions."""
    if value in (None, []):
        return []
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("project_ids must be an array of at most 100 UUIDs")
    try:
        values = [str(uuid.UUID(str(item))) for item in value]
    except (ValueError, TypeError, AttributeError):
        raise ValueError("project_ids must contain UUIDs.")
    return list(dict.fromkeys(values))


def authenticated(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        reason = _session_csrf_failure(request)
        if reason is not None:
            return _error(request, "csrf_failed", str(reason), 403)
        principal = _principal(request)
        if principal is None:
            return _unauthorized(request)
        request.principal = principal
        try:
            return view(request, *args, **kwargs)
        except Http404:
            return _error(request, "not_found", "Object not found.", 404)
        except (PermissionError, PermissionDenied) as exc:
            return _error(request, "forbidden", str(exc) or "Insufficient permission.", 403)
        except IntegrityError:
            return _error(
                request, "conflict", "The requested change conflicts with current data.", 409
            )

    return wrapped


def endpoint(view):
    @wraps(view)
    @csrf_exempt
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except ValueError as exc:
            return _error(request, "validation_error", str(exc), 422)
        except Http404:
            return _error(request, "not_found", "Object not found.", 404)
        except (PermissionError, PermissionDenied) as exc:
            return _error(request, "forbidden", str(exc), 403)
        except IntegrityError:
            return _error(
                request, "conflict", "The requested change conflicts with current data.", 409
            )

    return wrapped


def _json(view=None, auth=True):
    if view is None:
        return lambda actual: _json(actual, auth=auth)
    return authenticated(endpoint(view)) if auth else endpoint(view)


def _require(principal, project, role=ProjectMembership.Role.VIEWER, scope=None):
    try:
        return authorize_project(project, principal, minimum_role=role, scope=scope)
    except (Http404,):
        raise
    except ValidationError as exc:
        raise PermissionError(str(exc))


def _project(request, project_id, role=ProjectMembership.Role.VIEWER, scope=None):
    try:
        project = Project.objects.get(pk=project_id)
    except (Project.DoesNotExist, ValueError):
        raise Http404
    _require(request.principal, project, role, scope)
    return project


def _iso(value):
    return value.isoformat() if value else None


def _limit(request, default, maximum=100):
    try:
        value = int(request.GET.get("limit", str(default)))
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer")
    return min(max(value, 1), maximum)


def _decode_cursor(request):
    """Parse the opaque ``<timestamp>|<uuid>`` keyset cursor, or ``None``.

    Base64url so the timezone offset's ``+`` survives a query string.
    """
    raw = str(request.GET.get("cursor", ""))[:400]
    if not raw:
        return None
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
        stamp, _, ident = decoded.rpartition("|")
        return datetime.fromisoformat(stamp), uuid.UUID(ident)
    except (TypeError, ValueError, UnicodeDecodeError):
        raise ValueError("cursor is not a valid cursor")


def _encode_cursor(row, field):
    raw = f"{getattr(row, field).isoformat()}|{row.pk}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _keyset_page(queryset, request, field, *, default_limit, maximum=100):
    """Descending keyset page over ``(field, id)``; returns ``(rows, next_cursor)``.

    Ordering is newest-first and the tiebreak on ``id`` makes the page boundary
    stable, so a row is never skipped when several share a timestamp.
    """
    limit = _limit(request, default_limit, maximum)
    cursor = _decode_cursor(request)
    if cursor:
        stamp, ident = cursor
        queryset = queryset.filter(
            Q(**{f"{field}__lt": stamp}) | Q(**{field: stamp, "id__lt": ident})
        )
    rows = list(queryset.order_by(f"-{field}", "-id")[: limit + 1])
    if len(rows) > limit:
        return rows[:limit], _encode_cursor(rows[limit - 1], field)
    return rows, ""


def _spend_writes(request, amount=1, *, destructive=False):
    """Charge an agent write budget; returns a 429 response or ``None``.

    Human sessions are not charged — ``consume_mutation`` ignores a null token.
    """
    token = getattr(request.principal, "token", None)
    blocked, retry_after = consume_mutation(
        token.pk if token else None, amount=amount, destructive=destructive
    )
    if blocked:
        return _rate_limited(
            request,
            retry_after,
            "This agent has used its write budget. Try again later.",
        )
    return None


def user_json(user, private=False):
    result = {"id": str(user.pk), "email": user.email}
    profile = getattr(user, "profile", None)
    result["display_name"] = profile.display_name if profile else ""
    if private:
        result["is_active"] = user.is_active
    return result


def profile_json(profile):
    return {
        "id": str(profile.user_id),
        "display_name": profile.display_name,
        "timezone": profile.timezone,
        "bio": profile.bio,
        "details": profile.personal_details,
    }


def project_json(project, membership=None):
    membership = membership or project.memberships.filter(user_id=project.creator_id).first()
    return {
        "id": str(project.pk),
        "name": project.name,
        "slug": project.slug,
        "description": project.description,
        "prompt": project.prompt,
        "criteria": project.criteria,
        "status": project.status,
        "role": membership.role if membership else None,
        "prompt_revision": project.prompt_revision,
        "latest_change_sequence": project.latest_change_sequence,
        "created_at": _iso(project.created_at),
        "updated_at": _iso(project.updated_at),
    }


def lead_json(lead, viewer=None, include_interest=True):
    names = []
    interested = False
    if include_interest:
        # Interest intentionally survives membership removal, but removed
        # members must not remain visible in group projections.  It becomes
        # visible again if the membership is later restored.
        interests = lead.interests.select_related("user__profile").filter(
            user__project_memberships__project_id=lead.project_id
        ).distinct()
        viewer_user = getattr(viewer, "user", viewer)
        if viewer_user is not None:
            interested = interests.filter(user=viewer_user).exists()
        for interest in interests:
            profile = getattr(interest.user, "profile", None)
            names.append(profile.display_name if profile and profile.display_name else "Member")
    return {
        "id": str(lead.pk),
        "project_id": str(lead.project_id),
        "source": lead.source,
        "source_listing_id": lead.source_listing_id,
        "url": lead.canonical_url,
        "canonical_url": lead.canonical_url,
        "source_url": lead.source_url,
        "title": lead.title,
        "summary": lead.summary,
        "location": lead.location,
        "price_display": lead.price_display,
        "price_amount": str(lead.price_amount) if lead.price_amount is not None else None,
        "currency": lead.price_currency,
        "price_currency": lead.price_currency,
        "availability": lead.availability,
        "housing_type": lead.housing_type,
        "date_confidence": lead.date_confidence,
        "parks": lead.park_notes,
        "park_notes": lead.park_notes,
        "attributes": lead.attributes,
        "verification_notes": lead.verification_notes,
        "status": lead.status,
        "trashed_at": _iso(lead.trashed_at),
        "trashed_by": (
            getattr(getattr(lead.trashed_by, "profile", None), "display_name", "")
            or "Member"
            if lead.trashed_by_id
            else None
        ),
        "interested_users": names,
        "interest_count": len(names),
        "is_interested": interested,
        "comment_count": lead.comments.filter(deleted_at__isnull=True).count(),
        "revision": lead.revision,
        "updated_at": _iso(lead.updated_at),
        "created_at": _iso(lead.created_at),
    }


def run_json(run):
    return {
        "id": str(run.pk),
        "project_id": str(run.project_id),
        "status": run.status,
        "agent_label": run.agent_label,
        "prompt_revision": run.prompt_revision,
        "prompt_snapshot": run.prompt_snapshot,
        "criteria_snapshot": run.criteria_snapshot,
        "lease_expires_at": _iso(run.lease_expires_at),
        "attempt_count": run.attempt_count,
        "input_cursor": run.input_cursor,
        "output_cursor": run.output_cursor,
        "continuation": run.continuation,
        "result_counts": run.result_counts,
        "summary": run.summary,
        "created_at": _iso(run.created_at),
        "completed_at": _iso(run.completed_at),
    }


def _completion_matches(run, data, continuation, result_counts):
    """Compare a retry with the durable completion representation."""
    return (
        run.status == data.get("status")
        and run.output_cursor == str(data.get("output_cursor", ""))[:500]
        and run.continuation == continuation
        and run.result_counts == result_counts
        and run.summary == str(data.get("summary", ""))[:10000]
    )


# The run state schema is CLOSED.  Nothing here can carry free text that the
# next run reads as guidance: that is how untrusted listing prose launders
# itself into trusted first-party memory.  Adding a field is a security review.
CONTINUATION_STRINGS = {"worker": 120}
CONTINUATION_INTS = {"protocol": 16, "deferred_batches": 10_000}
CONTINUATION_SLUG_LISTS = ("lanes_owned", "needs_local", "needs_human")
CONTINUATION_KEYS = (
    set(CONTINUATION_STRINGS) | set(CONTINUATION_INTS) | set(CONTINUATION_SLUG_LISTS) | {"lanes"}
)
# Accepted and dropped for one release.  ``next_query`` is the laundering field.
CONTINUATION_DEPRECATED = {"next_query"}
LANE_STATUSES = {
    "ok",
    "empty",
    "blocked",
    "error",
    "deferred",
    "skipped",
    "skipped_needs_local",
    "skipped_needs_human",
}
LANE_STRINGS = {"lane": 80, "covered_through": 64}
LANE_INTS = {"items_seen": 1_000_000, "items_new": 1_000_000}
LANE_KEYS = set(LANE_STRINGS) | set(LANE_INTS) | {"status"}
RESULT_COUNT_KEYS = {
    "created",
    "updated",
    "unchanged",
    "conflicts",
    "trashed",
    "restored",
    "sources_ok",
    "sources_blocked",
    "suspected_injection",
    "urls_refused",
}
MAX_LANES = 64


def _slug(value, field, maximum=80):
    text = str(value or "").strip()
    if not text or len(text) > maximum:
        raise ValueError(f"{field} must be 1 to {maximum} characters")
    if any(ch.isspace() for ch in text):
        raise ValueError(f"{field} must not contain whitespace")
    return text


def _bounded_int(value, field, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < 0 or value > maximum:
        raise ValueError(f"{field} must be between 0 and {maximum}")
    return value


def _lane_entry(value):
    if not isinstance(value, dict):
        raise ValueError("continuation.lanes entries must be objects")
    unknown = sorted(set(value) - LANE_KEYS)
    if unknown:
        raise ValueError(f"continuation.lanes entries reject unknown fields: {', '.join(unknown)}")
    entry = {"lane": _slug(value.get("lane"), "continuation.lanes[].lane")}
    status = str(value.get("status", "")).strip()
    if status not in LANE_STATUSES:
        raise ValueError(
            "continuation.lanes[].status must be one of " + ", ".join(sorted(LANE_STATUSES))
        )
    entry["status"] = status
    for field, maximum in LANE_STRINGS.items():
        if field in value and field != "lane":
            entry[field] = _slug(value[field], f"continuation.lanes[].{field}", maximum)
    for field, maximum in LANE_INTS.items():
        if field in value:
            entry[field] = _bounded_int(value[field], f"continuation.lanes[].{field}", maximum)
    return entry


def _validate_continuation(value):
    """Return the normalized continuation, or raise. Unknown keys are rejected."""
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError("continuation must be an object")
    unknown = sorted(set(value) - CONTINUATION_KEYS - CONTINUATION_DEPRECATED)
    if unknown:
        raise ValueError(f"continuation rejects unknown fields: {', '.join(unknown)}")
    result = {}
    for field, maximum in CONTINUATION_STRINGS.items():
        if field in value:
            result[field] = _slug(value[field], f"continuation.{field}", maximum)
    for field, maximum in CONTINUATION_INTS.items():
        if field in value:
            result[field] = _bounded_int(value[field], f"continuation.{field}", maximum)
    for field in CONTINUATION_SLUG_LISTS:
        if field in value:
            items = value[field]
            if not isinstance(items, list) or len(items) > MAX_LANES:
                raise ValueError(f"continuation.{field} must be a list of at most {MAX_LANES} lanes")
            result[field] = [_slug(item, f"continuation.{field}[]") for item in items]
    if "lanes" in value:
        lanes = value["lanes"]
        if not isinstance(lanes, list) or len(lanes) > MAX_LANES:
            raise ValueError(f"continuation.lanes must be a list of at most {MAX_LANES} entries")
        result["lanes"] = [_lane_entry(lane) for lane in lanes]
    return result


def _validate_result_counts(value):
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError("result_counts must be an object")
    unknown = sorted(set(value) - RESULT_COUNT_KEYS)
    if unknown:
        raise ValueError(f"result_counts rejects unknown fields: {', '.join(unknown)}")
    return {
        key: _bounded_int(value[key], f"result_counts.{key}", 1_000_000) for key in sorted(value)
    }


def _request_hash(data):
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reserve_idempotency(request, principal, key, data):
    """Reserve/replay a durable write response inside the caller's transaction."""
    now = timezone.now()
    digest = _request_hash(data)
    record = (
        IdempotencyKey.objects.select_for_update()
        .filter(
            user=principal.user,
            token=principal.token,
            endpoint=request.path[:200],
            key=key,
        )
        .first()
    )
    if record:
        if record.expires_at > now and record.request_hash != digest:
            return None, _error(
                request,
                "idempotency_key_reused",
                "The idempotency key was already used with a different request.",
                409,
            )
        if record.expires_at > now and record.response_body is not None:
            return record, _response(request, record.response_body, record.response_status or 200)
        record.request_hash = digest
        record.response_body = None
        record.response_status = None
        record.expires_at = now + timedelta(days=7)
        record.save(update_fields=["request_hash", "response_body", "response_status", "expires_at"])
        return record, None
    record = IdempotencyKey.objects.create(
        user=principal.user,
        token=principal.token,
        endpoint=request.path[:200],
        key=key,
        request_hash=digest,
        expires_at=now + timedelta(days=7),
    )
    return record, None


@_json(auth=False)
def register(request):
    if request.method != "POST":
        return _error(request, "method_not_allowed", "POST required", 405)
    if not getattr(settings, "ALLOW_PUBLIC_SIGNUP", False):
        return _error(request, "registration_disabled", "Registration is disabled.", 403)
    data, error = _data(request)
    if error:
        blocked, retry_after = consume_auth_attempt(request_keys(request))
        if blocked:
            return _rate_limited(request, retry_after)
        return error
    email = normalize_email(str(data.get("email", "")))
    password = str(data.get("password", ""))
    display_name = str(data.get("display_name", "")).strip()
    keys = request_keys(request, email)
    blocked, retry_after = consume_auth_attempt(keys)
    if blocked:
        return _rate_limited(request, retry_after)
    if len(password) < 12 or "@" not in email or not display_name:
        raise ValueError(
            "A valid email, nickname, and password of at least 12 characters are required."
        )
    try:
        validate_password(password, User(email=email))
    except Exception as exc:
        # Password validators expose only a safe validation message; never
        # return a stack trace or account existence detail.
        raise ValueError(str(exc)) from exc
    if User.objects.filter(email=email).exists():
        # Do not disclose whether an account exists.  Registration clients can
        # treat this generic validation response the same as any other failed
        # registration (invitation flows do not depend on this response).
        return _error(request, "registration_unavailable", "Unable to register with these details.", 422)
    user = User.objects.create_user(email=email, password=password)
    Profile.objects.create(user=user, display_name=display_name[:120])
    reset_auth_attempts(keys)
    return _response(request, user_json(user), 201)


@_json(auth=False)
def token_exchange(request):
    # Off by default.  This is the endpoint whose documented happy path begins
    # "what is your Homing email and password?"; agents pair instead.
    if not getattr(settings, "ALLOW_PASSWORD_TOKEN_EXCHANGE", False):
        return _error(request, "not_found", "Object not found.", 404)
    if request.method != "POST":
        return _error(request, "method_not_allowed", "POST required", 405)
    data, error = _data(request)
    if error:
        blocked, retry_after = consume_auth_attempt(request_keys(request))
        if blocked:
            return _rate_limited(request, retry_after)
        return error
    email = normalize_email(str(data.get("email", "")))
    keys = request_keys(request, email)
    blocked, retry_after = consume_auth_attempt(keys)
    if blocked:
        return _rate_limited(request, retry_after)
    user = authenticate(
        request,
        username=email,
        password=data.get("password", ""),
    )
    if not user or not user.is_active:
        # Consume has already recorded the failed attempt.  Keep the response
        # generic so this endpoint cannot enumerate accounts.
        return _error(request, "unauthorized", "Invalid credentials.", 401)
    reset_auth_attempts(keys)
    scopes = set(
        data.get("scopes")
        or [
            "profile:read",
            "projects:read",
            "leads:read",
            "leads:write",
            "comments:read",
            "comments:write",
            "interest:read",
            "interest:write",
            "runs:write",
        ]
    )
    if not scopes.issubset(SCOPES):
        raise ValueError("Unknown scope.")
    project_ids = _project_ids(data.get("project_ids"))
    token, raw = create_agent_token(
        user=user,
        name=str(data.get("name") or "password exchange")[:120],
        scopes=scopes,
        project_ids=project_ids,
    )
    return _response(
        request,
        {
            "id": str(token.pk),
            "token": raw,
            "scopes": token.scopes,
            "project_ids": token.project_ids,
            "expires_at": _iso(token.expires_at),
        },
        200,
    )


@authenticated
@endpoint
def tokens(request):
    _human_only(request.principal)
    if request.method == "GET":
        if request.principal.token and "profile:read" not in request.principal.token.scopes:
            return _error(request, "forbidden", "Token lacks the required scope.", 403)
        return _response(
            request,
            {
                "items": [
                    {
                        "id": str(t.pk),
                        "name": t.name,
                        "prefix": t.token_prefix,
                        "scopes": t.scopes,
                        "project_ids": t.project_ids,
                        "expires_at": _iso(t.expires_at),
                        "revoked_at": _iso(t.revoked_at),
                    }
                    for t in request.principal.user.agent_tokens.all()
                ]
            },
        )
    if request.method != "POST":
        return _error(request, "method_not_allowed", "GET or POST required", 405)
    data, error = _data(request)
    if error:
        return error
    scopes = set(data.get("scopes") or [])
    if not data.get("name") or not scopes.issubset(SCOPES) or not scopes:
        raise ValueError("name and valid scopes are required")
    expires = timezone.now() + timedelta(days=getattr(settings, "AGENT_TOKEN_DEFAULT_DAYS", 90))
    token, raw = create_agent_token(
        user=request.principal.user,
        name=str(data["name"])[:120],
        scopes=scopes,
        project_ids=_project_ids(data.get("project_ids")),
        expires_at=expires,
    )
    return _response(
        request,
        {
            "id": str(token.pk),
            "token": raw,
            "scopes": token.scopes,
            "project_ids": token.project_ids,
            "expires_at": _iso(token.expires_at),
        },
        201,
    )


@authenticated
@endpoint
def token_detail(request, token_id):
    if request.method != "DELETE":
        return _error(request, "method_not_allowed", "DELETE required", 405)
    token = request.principal.user.agent_tokens.filter(pk=token_id).first()
    if not token:
        raise Http404
    revoke_agent_token(token)
    return _response(request, None, 204)


def _link_error(request, code, message, retry_after=None):
    """The four device-code outcomes, in Homing's standard error envelope."""
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    return _error(request, code, message, 400, headers=headers)


def _cadence(value):
    if value in (None, ""):
        return None
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        raise ValueError("requested_cadence_minutes must be an integer")
    if minutes < 1 or minutes > 10080:
        raise ValueError("requested_cadence_minutes must be between 1 and 10080")
    return minutes


@_json(auth=False)
def agent_link_start(request):
    """Begin device-code pairing.  Unauthenticated: the agent has no credential yet."""
    if request.method != "POST":
        return _error(request, "method_not_allowed", "POST required", 405)
    # Charge the IP before parsing so malformed floods count too.  Links live
    # ten minutes, so this window also caps concurrent pendings per address.
    blocked, retry_after = consume_pairing(request)
    if blocked:
        return _rate_limited(request, retry_after, "Too many pairing attempts. Try again later.")
    data, error = _data(request)
    if error:
        return error
    label = str(data.get("agent_label", "")).strip()[:120]
    if not label:
        raise ValueError("agent_label is required")
    note = str(data.get("environment_note", "")).strip()[:200]
    cadence = _cadence(data.get("requested_cadence_minutes"))
    now = timezone.now()
    AgentLink.objects.filter(status=AgentLink.Status.PENDING, expires_at__lte=now).update(
        status=AgentLink.Status.EXPIRED
    )
    device_code = secrets.token_urlsafe(32)
    link = None
    for _ in range(8):
        code = new_user_code()
        if AgentLink.objects.filter(
            user_code=code, status=AgentLink.Status.PENDING, expires_at__gt=now
        ).exists():
            continue
        link = AgentLink.objects.create(
            device_code_hash=device_code_digest(device_code),
            user_code=code,
            agent_label=label,
            environment_note=note,
            requested_cadence_minutes=cadence,
            expires_at=now + AGENT_LINK_TTL,
            interval_seconds=AGENT_LINK_INTERVAL_SECONDS,
        )
        break
    if link is None:
        return _error(request, "conflict", "Could not allocate a pairing code. Try again.", 409)
    link.audit("created", None, request_id=_request_id(request))
    origin = _public_origin(request)
    return _response(
        request,
        {
            "device_code": device_code,
            "user_code": link.user_code,
            "verification_uri": f"{origin}/link/",
            "verification_uri_complete": f"{origin}/link/?code={link.user_code}",
            "expires_in": int(AGENT_LINK_TTL.total_seconds()),
            "interval": link.interval_seconds,
        },
        201,
    )


@_json(auth=False)
def agent_link_poll(request):
    """Exchange an approved device code for a bearer token, exactly once."""
    if request.method != "POST":
        return _error(request, "method_not_allowed", "POST required", 405)
    data, error = _data(request)
    if error:
        return error
    device_code = str(data.get("device_code", ""))[:512]
    if not device_code:
        raise ValueError("device_code is required")
    request_id = _request_id(request)
    with transaction.atomic():
        link = (
            AgentLink.objects.select_for_update()
            .filter(device_code_hash=device_code_digest(device_code))
            .first()
        )
        if link is None:
            # Never distinguish "wrong code" from "denied": an unknown device
            # code must not be an oracle for guessing live ones.
            return _link_error(request, "access_denied", "This pairing was not approved.")
        now = timezone.now()
        if link.last_polled_at and (now - link.last_polled_at).total_seconds() < link.interval_seconds:
            link.poll_count += 1
            link.last_polled_at = now
            link.save(update_fields=["poll_count", "last_polled_at"])
            return _link_error(
                request,
                "slow_down",
                f"Poll no more than once every {link.interval_seconds} seconds.",
                link.interval_seconds,
            )
        link.poll_count += 1
        link.last_polled_at = now
        link.save(update_fields=["poll_count", "last_polled_at"])
        if link.status == AgentLink.Status.PENDING and link.is_expired:
            link.expire(request_id=request_id)
        if link.status == AgentLink.Status.EXPIRED:
            return _link_error(request, "expired_token", "This pairing request expired.")
        if link.status == AgentLink.Status.DENIED:
            return _link_error(request, "access_denied", "This pairing was not approved.")
        if link.status == AgentLink.Status.CONSUMED:
            # A device code buys exactly one token.  A second poll is either a
            # retry after the token was already delivered, or a replay.
            return _link_error(request, "access_denied", "This pairing was already used.")
        if link.status == AgentLink.Status.PENDING:
            return _link_error(
                request, "authorization_pending", "Waiting for approval in Homing.", link.interval_seconds
            )
        if link.approved_by is None or not link.approved_by.is_active:
            return _link_error(request, "access_denied", "This pairing was not approved.")
        token, raw = create_paired_agent_token(
            user=link.approved_by,
            name=link.agent_label or "paired agent",
            environment_note=link.environment_note,
            expected_cadence_minutes=link.requested_cadence_minutes,
        )
        link.status = AgentLink.Status.CONSUMED
        link.issued_token = token
        link.save(update_fields=["status", "issued_token"])
        link.audit("consumed", link.approved_by, {"token_id": str(token.pk)}, request_id=request_id)
    return _response(
        request,
        {"token": raw, "expires_at": _iso(token.expires_at), "scopes": token.scopes},
    )


@authenticated
@endpoint
def me(request):
    if request.method != "GET":
        return _error(request, "method_not_allowed", "GET required", 405)
    return _response(request, user_json(request.principal.user, True))


@authenticated
@endpoint
def me_token(request):
    """Introspect the calling credential so a runtime can warn about expiry."""
    if request.method != "GET":
        return _error(request, "method_not_allowed", "GET required", 405)
    token = request.principal.token
    return _response(
        request,
        {
            "id": str(token.pk) if token else None,
            "name": token.name if token else "",
            "scopes": token.scopes if token else sorted(SCOPES),
            "expires_at": _iso(token.expires_at) if token else None,
            "last_used_at": _iso(token.last_used_at) if token else None,
            "agent_paused_until": _iso(_agent_paused_until(request.principal.user)),
        },
    )


@authenticated
@endpoint
def profile(request):
    profile_obj, _ = Profile.objects.get_or_create(user=request.principal.user)
    if request.method == "GET":
        return _response(request, profile_json(profile_obj))
    if request.method != "PATCH":
        return _error(request, "method_not_allowed", "GET or PATCH required", 405)
    _human_only(request.principal)
    data, error = _data(request)
    if error:
        return error
    for key, field, max_len in (
        ("display_name", "display_name", 120),
        ("timezone", "timezone", 64),
        ("bio", "bio", 5000),
        ("details", "personal_details", 50000),
    ):
        if key in data:
            if key == "details" and not isinstance(data[key], dict):
                raise ValueError("details must be an object")
            if isinstance(data[key], str) and len(data[key]) > max_len:
                raise ValueError(f"{key} is too long")
            if key == "display_name" and not str(data[key]).strip():
                raise ValueError("display_name cannot be empty")
            setattr(profile_obj, field, data[key])
    profile_obj.save()
    return _response(request, profile_json(profile_obj))


@authenticated
@endpoint
def saved_prompts(request):
    if request.method == "GET":
        return _response(
            request,
            {
                "items": [
                    {
                        "id": str(p.pk),
                        "title": p.title,
                        "prompt": p.prompt,
                        "updated_at": _iso(p.updated_at),
                    }
                    for p in request.principal.user.saved_prompts.all()
                ]
            },
        )
    if request.method != "POST":
        return _error(request, "method_not_allowed", "GET or POST required", 405)
    _human_only(request.principal)
    data, error = _data(request)
    if error:
        return error
    if not data.get("title") or not data.get("prompt"):
        raise ValueError("title and prompt are required")
    p = SavedPrompt.objects.create(
        user=request.principal.user,
        title=str(data["title"])[:200],
        prompt=str(data["prompt"])[:30000],
    )
    return _response(
        request,
        {"id": str(p.pk), "title": p.title, "prompt": p.prompt, "updated_at": _iso(p.updated_at)},
        201,
    )


def _agent_paused_until(user):
    """Read-only: never create a Profile row from a GET."""
    return (
        Profile.objects.filter(user=user)
        .values_list("agent_paused_until", flat=True)
        .first()
    )


def _project_list(request):
    memberships = ProjectMembership.objects.filter(user=request.principal.user).select_related(
        "project"
    )
    return {
        "items": [
            project_json(m.project, m)
            for m in memberships
            if m.project.status != Project.Status.TRASHED
            and not project_is_restricted(m.project, request.principal.token)
        ],
        # Carried here so a scheduled runtime learns it is paused on its very
        # first call and exits without touching anything else.
        "agent_paused_until": _iso(_agent_paused_until(request.principal.user)),
    }


@authenticated
@endpoint
def my_projects(request):
    if request.method != "GET":
        return _error(request, "method_not_allowed", "GET required", 405)
    return _response(request, _project_list(request))


@authenticated
@endpoint
def projects(request):
    if request.method == "GET":
        return _response(request, _project_list(request))
    if request.method != "POST":
        return _error(request, "method_not_allowed", "GET or POST required", 405)
    _human_only(request.principal)
    data, error = _data(request)
    if error:
        return error
    if not data.get("name") or "prompt" not in data:
        raise ValueError("name and prompt are required")
    base = slugify(str(data["name"]))[:210] or "project"
    slug, n = base, 2
    while Project.objects.filter(slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    with transaction.atomic():
        project = Project.objects.create(
            name=str(data["name"])[:200],
            slug=slug,
            description=str(data.get("description", ""))[:10000],
            prompt=str(data["prompt"])[:30000],
            criteria=data.get("criteria") or {},
            creator=request.principal.user,
            prompt_revision=1,
        )
        ProjectMembership.objects.create(
            project=project, user=request.principal.user, role=ProjectMembership.Role.OWNER
        )
        PromptRevision.objects.create(
            project=project,
            revision=1,
            prompt=project.prompt,
            criteria=project.criteria,
            editor=request.principal.user,
        )
        append_change(project, "project.created", "project", str(project.pk), {}, request.principal)
    return _response(request, project_json(project), 201)


@authenticated
@endpoint
def project_detail(request, project_id):
    project = _project(request, project_id, scope="projects:read")
    if request.method == "GET":
        return _response(
            request, project_json(project, project.memberships.get(user=request.principal.user))
        )
    if request.method != "PATCH":
        return _error(request, "method_not_allowed", "GET or PATCH required", 405)
    _human_only(request.principal)
    # Project metadata is shared project content; every collaborator may edit it.
    data, error = _data(request)
    if error:
        return error
    with transaction.atomic():
        project = Project.objects.select_for_update().get(pk=project.pk)
        for field in ("name", "description"):
            if field in data:
                setattr(project, field, str(data[field])[: 10000 if field == "description" else 200])
        if "status" in data:
            if data["status"] not in ("active", "trashed"):
                raise ValueError("invalid status")
            project.status = data["status"]
        project.save()
        append_change(
            project,
            "project.updated",
            "project",
            str(project.pk),
            {"fields": sorted(k for k in data if k in {"name", "description", "status"})},
            request.principal,
        )
    return _response(
        request, project_json(project, project.memberships.get(user=request.principal.user))
    )


@authenticated
@endpoint
def members(request, project_id):
    project = _project(request, project_id, scope="projects:read")
    if request.method == "GET":
        return _response(
            request,
            {
                "items": [
                    dict(
                        {
                            "user_id": str(m.user_id),
                            "display_name": (
                                getattr(getattr(m.user, "profile", None), "display_name", "")
                                or "Member"
                            ),
                            "role": m.role,
                        },
                        email=m.user.email,
                    )
                    for m in project.memberships.select_related("user__profile")
                ]
            },
        )
    if request.method not in ("PATCH", "DELETE"):
        return _error(request, "method_not_allowed", "GET, PATCH, or DELETE required", 405)
    _human_only(request.principal)
    assert_owner(project, request.principal)
    data, error = _data(request)
    if error:
        return error
    try:
        member_id = data["user_id"]
    except KeyError:
        raise Http404
    if request.method == "DELETE":
        try:
            remove_project_member(
                project, member_user_id=member_id, actor=request.principal
            )
        except ProjectMembership.DoesNotExist:
            raise Http404
        except FinalOwnerError:
            return _error(request, "final_owner", "A project must retain an owner.", 409)
        return _response(request, {}, 204)
    role = data.get("role")
    if role not in ProjectMembership.Role.values:
        raise ValueError("invalid role")
    with transaction.atomic():
        project = Project.objects.select_for_update().get(pk=project.pk)
        try:
            member = project.memberships.select_for_update().get(user_id=member_id)
        except ProjectMembership.DoesNotExist:
            raise Http404
        if (
            member.role == ProjectMembership.Role.OWNER
            and role != member.role
            and project.memberships.filter(role="owner").count() == 1
        ):
            return _error(request, "final_owner", "A project must retain an owner.", 409)
        member.role = role
        member.save(update_fields=["role"])
        append_change(
            project,
            "membership.role_changed",
            "membership",
            str(member.user_id),
            {"user_id": str(member.user_id), "role": role},
            request.principal,
        )
    return _response(request, {"user_id": str(member.user_id), "role": member.role})


@authenticated
@endpoint
def invitations(request, project_id):
    # Invitations are collaborative project content: every active member may
    # add another person.  Owner-only membership role administration remains
    # enforced by ``members`` below.
    project = _project(request, project_id, scope="projects:read")
    if request.method != "POST":
        return _error(request, "method_not_allowed", "POST required", 405)
    _human_only(request.principal)
    data, error = _data(request)
    if error:
        return error
    email, role = normalize_email(str(data.get("email", ""))), data.get("role", "viewer")
    if "@" not in email or role not in ("editor", "viewer"):
        raise ValueError("valid email and role are required")
    raw = secrets.token_urlsafe(32)
    with transaction.atomic():
        locked = Project.objects.select_for_update().get(pk=project.pk)
        invitation = ProjectInvitation.objects.create(
            project=locked,
            invited_email=email,
            role=role,
            inviter=request.principal.user,
            token_digest=digest_token(raw),
            expires_at=timezone.now() + timedelta(days=7),
        )
        append_change(
            locked,
            "invitation.created",
            "invitation",
            str(invitation.pk),
            {"role": role},
            request.principal,
        )
    send_invitation_email(invitation=invitation, raw_token=raw)
    return _response(
        request,
        {
            "id": str(invitation.pk),
            "role": invitation.role,
            "expires_at": _iso(invitation.expires_at),
            "invite_url": f"/invitations/{raw}/accept",
        },
        201,
    )


@authenticated
@endpoint
def accept_invitation(request, invitation_token):
    if request.method != "POST":
        return _error(request, "method_not_allowed", "POST required", 405)
    _human_only(request.principal)
    with transaction.atomic():
        invitation = (
            ProjectInvitation.objects.select_for_update()
            .filter(token_digest=digest_token(invitation_token))
            .first()
        )
        if not invitation or not invitation.is_pending:
            raise Http404
        if normalize_email(request.principal.user.email) != invitation.invited_email:
            # A valid invitation token is still a secret.  Do not let a
            # logged-in non-recipient use it as an oracle.
            raise Http404
        project = Project.objects.select_for_update().get(pk=invitation.project_id)
        if not invitation.inviter.is_active or not project.memberships.filter(
            user_id=invitation.inviter_id
        ).exists():
            raise Http404
        if not project.memberships.filter(user=request.principal.user).exists():
            ProjectMembership.objects.create(
                project=project, user=request.principal.user, role=invitation.role
            )
            append_change(
                project,
                "membership.joined",
                "membership",
                str(request.principal.user.pk),
                {"user_id": str(request.principal.user.pk), "role": invitation.role},
                request.principal,
            )
        invitation.accepted_at = timezone.now()
        invitation.save(update_fields=["accepted_at"])
        append_change(
            project,
            "invitation.accepted",
            "invitation",
            str(invitation.pk),
            {"user_id": str(request.principal.user.pk)},
            request.principal,
        )
    return _response(request, None, 204)


@authenticated
@endpoint
def prompt(request, project_id):
    project = _project(request, project_id, scope="prompts:read")
    if request.method == "GET":
        return _response(
            request,
            {
                "prompt": project.prompt,
                "criteria": project.criteria,
                "revision": project.prompt_revision,
                "updated_at": _iso(project.updated_at),
            },
        )
    if request.method != "PUT":
        return _error(request, "method_not_allowed", "GET or PUT required", 405)
    _human_only(request.principal)
    data, error = _data(request)
    if error:
        return error
    try:
        revision = update_project_prompt(
            project,
            editor=request.principal,
            prompt=str(data["prompt"]),
            criteria=data.get("criteria") or {},
            expected_revision=data.get("expected_revision"),
        )
    except PromptRevisionConflict as exc:
        return _error(
            request,
            "stale_write",
            "Prompt changed since it was read.",
            409,
            {"expected_revision": [f"current revision is {exc.current_revision}"]},
        )
    return _response(
        request,
        {
            "prompt": revision.prompt,
            "criteria": revision.criteria,
            "revision": revision.revision,
            "updated_at": _iso(revision.created_at),
        },
    )


@authenticated
@endpoint
def prompt_revisions(request, project_id):
    project = _project(request, project_id, scope="prompts:read")
    if request.method != "GET":
        return _error(request, "method_not_allowed", "GET required", 405)
    return _response(
        request,
        {
            "items": [
                {
                    "prompt": p.prompt,
                    "criteria": p.criteria,
                    "revision": p.revision,
                    "updated_at": _iso(p.created_at),
                }
                for p in project.prompt_revisions.all()
            ]
        },
    )


@authenticated
@endpoint
def changes(request, project_id):
    project = _project(request, project_id, scope="projects:read")
    if request.method != "GET":
        return _error(request, "method_not_allowed", "GET required", 405)
    try:
        cursor = int(request.GET.get("cursor", "0"))
    except ValueError:
        raise ValueError("cursor must be an opaque cursor")
    limit = min(max(int(request.GET.get("limit", "50")), 1), 100)
    qs = project.changes.filter(sequence__gt=cursor).order_by("sequence")[:limit]
    items = [
        {
            "sequence": c.sequence,
            "event_type": c.event_type,
            "object_type": c.object_type,
            "object_id": c.object_id,
            "payload": c.payload,
            "tombstone": c.tombstone,
            "occurred_at": _iso(c.created_at),
        }
        for c in qs
    ]
    next_cursor = items[-1]["sequence"] if items else cursor
    return _response(request, {"items": items, "next_cursor": str(next_cursor)})


def _run_for(request, project_id, run_id):
    project = _project(request, project_id, scope="projects:read")
    try:
        return project.search_runs.get(pk=run_id)
    except SearchRun.DoesNotExist:
        raise Http404


@authenticated
@endpoint
def search_runs(request, project_id):
    project = _project(
        request, project_id, scope="projects:read" if request.method == "GET" else "runs:write"
    )
    if request.method == "GET":
        # Newest first, always.  The multi-worker protocol reads this endpoint to
        # find "the most recent run for agent_label prefix X".
        qs = project.search_runs.all()
        prefix = str(request.GET.get("agent_label_prefix", ""))[:160]
        if prefix:
            qs = qs.filter(agent_label__startswith=prefix)
        rows, next_cursor = _keyset_page(qs, request, "created_at", default_limit=20)
        return _response(
            request,
            {"items": [run_json(r) for r in rows], "next_cursor": next_cursor, "ordering": "-created_at"},
        )
    if request.method != "POST":
        return _error(request, "method_not_allowed", "GET or POST required", 405)
    data, error = _data(request)
    if error:
        return error
    idem = request.headers.get("Idempotency-Key", "")[:200]
    if idem:
        existing = project.search_runs.filter(
            user=request.principal.user, idempotency_key=idem
        ).first()
        if existing:
            requested_label = str(data.get("agent_label", ""))[:160]
            requested_cursor = str(data.get("input_cursor", ""))[:500]
            if existing.agent_label != requested_label or existing.input_cursor != requested_cursor:
                return _error(
                    request,
                    "idempotency_key_reused",
                    "The idempotency key was already used with a different request.",
                    409,
                )
            return _response(request, run_json(existing), 201)
    with transaction.atomic():
        project = Project.objects.select_for_update().get(pk=project.pk)
        run = SearchRun.objects.create(
            project=project,
            user=request.principal.user,
            agent_token=request.principal.token,
            agent_label=str(data.get("agent_label", ""))[:160],
            prompt_revision=project.prompt_revision,
            prompt_snapshot=project.prompt,
            criteria_snapshot=project.criteria,
            input_cursor=str(data.get("input_cursor", ""))[:500],
            idempotency_key=idem,
        )
        append_change(
            project,
            "run.created",
            "search_run",
            str(run.pk),
            {"status": run.status, "prompt_revision": run.prompt_revision},
            request.principal,
        )
    return _response(request, run_json(run), 201)


@authenticated
@endpoint
def search_run_detail(request, project_id, run_id):
    run = _run_for(request, project_id, run_id)
    if request.method != "GET":
        return _error(request, "method_not_allowed", "GET required", 405)
    return _response(request, run_json(run))


def _check_run(request, project_id, run_id):
    project = _project(request, project_id, role=ProjectMembership.Role.VIEWER, scope="runs:write")
    # Serialize lease decisions on the project row.  Locking only the target
    # run allows two workers to claim different runs concurrently.
    project = Project.objects.select_for_update().get(pk=project.pk)
    try:
        run = project.search_runs.select_for_update().get(pk=run_id)
    except SearchRun.DoesNotExist:
        raise Http404
    return project, run


@authenticated
@endpoint
def claim_run(request, project_id, run_id):
    if request.method != "POST":
        return _error(request, "method_not_allowed", "POST required", 405)
    with transaction.atomic():
        project, run = _check_run(request, project_id, run_id)
        now = timezone.now()
        active = (
            project.search_runs.filter(status__in=["claimed", "running"], lease_expires_at__gt=now)
            .exclude(pk=run.pk)
            .exists()
        )
        if active:
            return _error(
                request, "run_already_claimed", "Another run holds the project lease.", 409
            )
        if run.status in ("completed", "failed", "cancelled"):
            return _error(request, "run_not_claimable", "Run is already finished.", 409)
        run.status = SearchRun.Status.CLAIMED
        run.lease_owner = f"{request.principal.user.pk}:{request.principal.token.pk if request.principal.token else 'session'}"
        run.lease_expires_at = now + timedelta(minutes=5)
        run.claim_token = secrets.token_urlsafe(32)
        run.attempt_count += 1
        run.started_at = run.started_at or now
        run.save(
            update_fields=[
                "status",
                "lease_owner",
                "lease_expires_at",
                "claim_token",
                "attempt_count",
                "started_at",
                "updated_at",
            ]
        )
        append_change(
            project,
            "run.claimed",
            "search_run",
            str(run.pk),
            {"status": run.status, "attempt_count": run.attempt_count},
            request.principal,
        )
    return _response(
        request, {"claim_token": run.claim_token, "lease_expires_at": _iso(run.lease_expires_at)}
    )


def _claim_data(request):
    data, error = _data(request)
    if error:
        return None, error
    if not data.get("claim_token"):
        raise ValueError("claim_token is required")
    return data, None


@authenticated
@endpoint
def heartbeat_run(request, project_id, run_id):
    if request.method != "POST":
        return _error(request, "method_not_allowed", "POST required", 405)
    data, error = _claim_data(request)
    if error:
        return error
    with transaction.atomic():
        project, run = _check_run(request, project_id, run_id)
        if (
            run.claim_token != data["claim_token"]
            or not run.lease_expires_at
            or run.lease_expires_at <= timezone.now()
        ):
            return _error(request, "invalid_claim", "Claim token is invalid or expired.", 409)
        run.status = SearchRun.Status.RUNNING
        run.lease_expires_at = timezone.now() + timedelta(minutes=5)
        run.save(update_fields=["status", "lease_expires_at", "updated_at"])
        append_change(
            project,
            "run.heartbeat",
            "search_run",
            str(run.pk),
            {"status": run.status},
            request.principal,
        )
    return _response(
        request, {"lease_expires_at": _iso(run.lease_expires_at), "status": run.status}
    )


@authenticated
@endpoint
def complete_run(request, project_id, run_id):
    if request.method != "POST":
        return _error(request, "method_not_allowed", "POST required", 405)
    data, error = _claim_data(request)
    if error:
        return error
    if data.get("status") not in ("completed", "failed"):
        raise ValueError("status must be completed or failed")
    idem = request.headers.get("Idempotency-Key", "")[:200]
    if not idem:
        raise ValueError("Idempotency-Key is required when completing a run")
    raw_continuation = data.get("continuation") or {}
    continuation = _validate_continuation(raw_continuation)
    result_counts = _validate_result_counts(data.get("result_counts"))
    deprecated = sorted(
        CONTINUATION_DEPRECATED & set(raw_continuation if isinstance(raw_continuation, dict) else {})
    )
    headers = (
        {
            "X-Homing-Deprecation": (
                "ignored continuation fields: " + ", ".join(deprecated) + "; they will be rejected"
            )
        }
        if deprecated
        else None
    )
    with transaction.atomic():
        project, run = _check_run(request, project_id, run_id)
        idempotency, replay = _reserve_idempotency(
            request, request.principal, idem, data
        )
        if replay is not None:
            return replay
        if run.status in ("completed", "failed"):
            if not _completion_matches(run, data, continuation, result_counts):
                return _error(
                    request,
                    "idempotency_key_reused",
                    "The run was already completed with a different payload.",
                    409,
                )
            body = run_json(run)
            idempotency.response_status = 200
            idempotency.response_body = body
            idempotency.save(update_fields=["response_status", "response_body"])
            return _response(request, body, headers=headers)
        if (
            run.claim_token != data["claim_token"]
            or not run.lease_expires_at
            or run.lease_expires_at <= timezone.now()
        ):
            return _error(request, "invalid_claim", "Claim token is invalid or expired.", 409)
        run.status = data["status"]
        run.output_cursor = str(data.get("output_cursor", ""))[:500]
        run.continuation = continuation
        run.result_counts = result_counts
        run.summary = str(data.get("summary", ""))[:10000]
        run.completed_at = timezone.now()
        run.lease_expires_at = None
        run.save(
            update_fields=[
                "status",
                "output_cursor",
                "continuation",
                "result_counts",
                "summary",
                "completed_at",
                "lease_expires_at",
                "updated_at",
            ]
        )
        append_change(
            project,
            "run.completed",
            "search_run",
            str(run.pk),
            {"status": run.status},
            request.principal,
        )
        body = run_json(run)
        idempotency.response_status = 200
        idempotency.response_body = body
        idempotency.save(update_fields=["response_status", "response_body"])
    return _response(request, body, headers=headers)


def _lead_for(request, project_id, lead_id, scope="leads:read", role=ProjectMembership.Role.VIEWER):
    project = _project(request, project_id, role=role, scope=scope)
    try:
        lead = project.leads.get(pk=lead_id)
    except Lead.DoesNotExist:
        raise Http404
    return project, lead


LEAD_FIELDS = {
    "source",
    "source_listing_id",
    "title",
    "summary",
    "location",
    "price_display",
    "price_amount",
    "currency",
    "price_currency",
    "availability",
    "housing_type",
    "date_confidence",
    "parks",
    "park_notes",
    "attributes",
    "verification_notes",
    "url",
    "canonical_url",
    "source_url",
}


def _lead_values(data, partial=False):
    if not partial and (
        not data.get("source")
        or not data.get("title")
        or not data.get("url", data.get("canonical_url"))
    ):
        raise ValueError("source, url, and title are required")
    vals = {}
    if "url" in data:
        vals["canonical_url"] = data["url"]
    for key in LEAD_FIELDS & set(data):
        target = {
            "url": "canonical_url",
            "currency": "price_currency",
            "parks": "park_notes",
            "canonical_url": "canonical_url",
        }.get(key, key)
        if (
            target in ("canonical_url", "source_url")
            and data[key]
            and urlsplit(str(data[key])).scheme not in ("http", "https")
        ):
            raise ValueError("Only HTTP(S) URLs are accepted.")
        vals[target] = data[key]
    if "housing_type" in vals and vals["housing_type"] not in Lead.HousingType.values:
        vals["housing_type"] = (
            "unknown" if vals["housing_type"] not in ("entire", "shared") else vals["housing_type"]
        )
    if "date_confidence" in vals and vals["date_confidence"] not in Lead.DateConfidence.values:
        raise ValueError("invalid date_confidence")
    if "price_currency" in vals:
        vals["price_currency"] = str(vals["price_currency"])[:3].upper()
    return vals


def _find_identity(project, data):
    source, source_id = str(data.get("source", "")), str(data.get("source_listing_id", ""))
    if source_id:
        return project.leads.filter(source=source, source_listing_id=source_id).first()
    url = data.get("url") or data.get("canonical_url")
    if url:
        return project.leads.filter(identity_hash=listing_identity_hash(str(url))).first()
    return None


@authenticated
@endpoint
def leads(request, project_id):
    project = _project(
        request,
        project_id,
        role=(
            ProjectMembership.Role.VIEWER
            if request.method == "GET"
            else ProjectMembership.Role.VIEWER
        ),
        scope="leads:read" if request.method == "GET" else "leads:write",
    )
    if request.method == "GET":
        qs = project.leads.all()
        status = request.GET.get("status", "active")
        if status in Lead.Status.values:
            qs = qs.filter(status=status)
        if request.GET.get("date_confidence"):
            qs = qs.filter(date_confidence=request.GET["date_confidence"])
        if request.GET.get("housing_type"):
            qs = qs.filter(housing_type=request.GET["housing_type"])
        if request.GET.get("q"):
            qs = qs.filter(title__icontains=request.GET["q"][:200]) | qs.filter(
                summary__icontains=request.GET["q"][:200]
            )
        interested = request.GET.get("interested_by")
        if interested == "me":
            qs = qs.filter(interests__user=request.principal.user)
        qs = qs.distinct().order_by("-updated_at")
        limit = min(max(int(request.GET.get("limit", "50")), 1), 100)
        return _response(
            request,
            {
                "items": [lead_json(lead, request.principal) for lead in qs[:limit]],
                "next_cursor": "",
            },
        )
    if request.method != "POST":
        return _error(request, "method_not_allowed", "GET or POST required", 405)
    data, error = _data(request)
    if error:
        return error
    vals = _lead_values(data)
    if Lead.objects.filter(
        project=project, source=vals["source"], source_listing_id=vals.get("source_listing_id", "")
    ).exists():
        return _error(
            request, "identity_conflict", "A lead with this source identity already exists.", 409
        )
    throttled = _spend_writes(request)
    if throttled:
        return throttled
    with transaction.atomic():
        locked = Project.objects.select_for_update().get(pk=project.pk)
        vals.update(project=locked, creator=request.principal.user)
        lead = Lead.objects.create(**vals)
        append_change(
            locked,
            "lead.created",
            "lead",
            str(lead.pk),
            {"revision": lead.revision},
            request.principal,
        )
    return _response(request, lead_json(lead), 201, {"ETag": lead.etag})


@authenticated
@endpoint
def bulk_upsert(request, project_id):
    project = _project(request, project_id, role=ProjectMembership.Role.VIEWER, scope="leads:write")
    if request.method != "POST":
        return _error(request, "method_not_allowed", "POST required", 405)
    data, error = _data(request)
    if error:
        return error
    items = data.get("items")
    if not isinstance(items, list) or not items or len(items) > 100:
        raise ValueError("items must contain 1 to 100 records")
    # Charged per record: a batch endpoint that costs one unit is not a budget.
    throttled = _spend_writes(request, len(items))
    if throttled:
        return throttled
    results = []
    for index, item in enumerate(items):
        try:
            vals = _lead_values(item, partial=False)
            lead = _find_identity(project, item)
            if lead and lead.status == Lead.Status.TRASHED:
                results.append(
                    {
                        "index": index,
                        "outcome": "conflict",
                        "error": {
                            "code": "lead_trashed",
                            "message": "Trashed leads are not silently restored.",
                        },
                    }
                )
                continue
            if lead:
                expected = item.get("if_match")
                if not expected:
                    results.append(
                        {
                            "index": index,
                            "outcome": "conflict",
                            "error": {"code": "if_match_required", "message": "if_match is required when updating an existing lead."},
                        }
                    )
                    continue
                if expected and expected.strip('"') != str(lead.revision):
                    results.append(
                        {
                            "index": index,
                            "outcome": "conflict",
                            "error": {"code": "stale_write", "message": "Revision mismatch."},
                        }
                    )
                    continue
                with transaction.atomic():
                    lead = Lead.objects.select_for_update().get(pk=lead.pk)
                    # Re-check after locking so an interleaved human edit
                    # cannot be silently overwritten.
                    if expected and expected.strip('"') != str(lead.revision):
                        results.append(
                            {
                                "index": index,
                                "outcome": "conflict",
                                "error": {"code": "stale_write", "message": "Revision mismatch."},
                            }
                        )
                        continue
                    changed = any(getattr(lead, k) != v for k, v in vals.items())
                    if changed:
                        for key, value in vals.items():
                            setattr(lead, key, value)
                        lead.revision += 1
                        lead.save()
                        append_change(
                            lead.project,
                            "lead.updated",
                            "lead",
                            str(lead.pk),
                            {"revision": lead.revision},
                            request.principal,
                        )
                        results.append({"index": index, "outcome": "updated", "lead": lead_json(lead)})
                    else:
                        results.append(
                            {"index": index, "outcome": "unchanged", "lead": lead_json(lead)}
                        )
            else:
                with transaction.atomic():
                    lead = Lead.objects.create(project=project, creator=request.principal.user, **vals)
                    append_change(
                        project,
                        "lead.created",
                        "lead",
                        str(lead.pk),
                        {"revision": lead.revision},
                        request.principal,
                    )
                    results.append({"index": index, "outcome": "created", "lead": lead_json(lead)})
        except (ValueError, IntegrityError) as exc:
            results.append(
                {
                    "index": index,
                    "outcome": "error",
                    "error": {"code": "validation_error", "message": str(exc)},
                }
            )
    return _response(request, {"results": results})


@authenticated
@endpoint
def lead_detail(request, project_id, lead_id):
    project, lead = _lead_for(
        request,
        project_id,
        lead_id,
        {"GET": "leads:read", "DELETE": "leads:destroy"}.get(request.method, "leads:write"),
        ProjectMembership.Role.VIEWER,
    )
    if request.method == "GET":
        return _response(request, lead_json(lead, request.principal), headers={"ETag": lead.etag})
    if request.method == "DELETE":
        data, error = _data(request)
        if error:
            return error
        if request.headers.get("If-Match") and request.headers["If-Match"].strip('"') != str(
            lead.revision
        ):
            return _error(request, "stale_write", "Lead changed since it was read.", 409)
        throttled = _spend_writes(request, destructive=True)
        if throttled:
            return throttled
        trash_lead(
            lead,
            actor=request.principal,
            comment=data.get("comment", data.get("reason", "")),
        )
        return _response(request, None, 204)
    if request.method != "PATCH":
        return _error(request, "method_not_allowed", "GET, PATCH, or DELETE required", 405)
    if not request.headers.get("If-Match"):
        return _error(request, "if_match_required", "If-Match is required for lead writes.", 409)
    if request.headers["If-Match"].strip('"') != str(lead.revision):
        return _error(request, "stale_write", "Lead changed since it was read.", 409)
    data, error = _data(request)
    if error:
        return error
    vals = _lead_values(data, partial=True)
    vals.pop("source", None)
    vals.pop("source_listing_id", None)
    throttled = _spend_writes(request)
    if throttled:
        return throttled
    with transaction.atomic():
        lead = Lead.objects.select_for_update().get(pk=lead.pk)
        if lead.revision != int(request.headers["If-Match"].strip('"')):
            return _error(request, "stale_write", "Lead changed since it was read.", 409)
        for key, value in vals.items():
            setattr(lead, key, value)
        lead.revision += 1
        lead.save()
        append_change(
            project,
            "lead.updated",
            "lead",
            str(lead.pk),
            {"revision": lead.revision},
            request.principal,
        )
    return _response(request, lead_json(lead), headers={"ETag": lead.etag})


@authenticated
@endpoint
def interest(request, project_id, lead_id):
    project, lead = _lead_for(request, project_id, lead_id, "interest:write")
    if request.method not in ("PUT", "DELETE"):
        return _error(request, "method_not_allowed", "PUT or DELETE required", 405)
    throttled = _spend_writes(request)
    if throttled:
        return throttled
    set_interest(lead, user=request.principal, interested=request.method == "PUT")
    return _response(request, None, 204)


def comment_json(comment):
    profile = getattr(comment.author, "profile", None)
    return {
        "id": comment.id,
        "body": comment.body,
        "author_id": str(comment.author_id),
        "author_display_name": profile.display_name if profile and profile.display_name else "Member",
        "created_at": _iso(comment.created_at),
        "edited_at": _iso(comment.edited_at),
        "deleted_at": _iso(comment.deleted_at),
    }


@authenticated
@endpoint
def comments(request, project_id, lead_id):
    project, lead = _lead_for(
        request,
        project_id,
        lead_id,
        "comments:read" if request.method == "GET" else "comments:write",
    )
    if request.method == "GET":
        return _response(
            request,
            {
                "items": [
                    comment_json(c)
                    for c in lead.comments.select_related("author__profile").filter(
                        deleted_at__isnull=True
                    )
                ]
            },
        )
    if request.method != "POST":
        return _error(request, "method_not_allowed", "GET or POST required", 405)
    data, error = _data(request)
    if error:
        return error
    body = str(data.get("body", ""))
    if not body.strip() or len(body) > 10000:
        raise ValueError("body must contain 1 to 10000 characters")
    throttled = _spend_writes(request)
    if throttled:
        return throttled
    with transaction.atomic():
        locked = Project.objects.select_for_update().get(pk=project.pk)
        comment = LeadComment.objects.create(lead=lead, author=request.principal.user, body=body)
        append_change(locked, "comment.created", "comment", str(comment.pk), {}, request.principal)
        append_audit(locked, "comment.created", "comment", str(comment.pk), {}, request.principal)
    return _response(request, comment_json(comment), 201)


@authenticated
@endpoint
def comment_detail(request, project_id, lead_id, comment_id):
    project, lead = _lead_for(request, project_id, lead_id, "comments:write")
    try:
        comment = lead.comments.get(pk=comment_id)
    except LeadComment.DoesNotExist:
        raise Http404
    if comment.deleted_at:
        raise Http404
    membership = project.memberships.get(user=request.principal.user)
    if (
        comment.author_id != request.principal.user.pk
        and membership.role != ProjectMembership.Role.OWNER
    ):
        raise PermissionError("Only the author or project owner can moderate comments.")
    if request.method == "PATCH":
        data, error = _data(request)
        if error:
            return error
        body = str(data.get("body", ""))
        if not body.strip() or len(body) > 10000:
            raise ValueError("body must contain 1 to 10000 characters")
        with transaction.atomic():
            locked = Project.objects.select_for_update().get(pk=project.pk)
            comment = LeadComment.objects.select_for_update().get(pk=comment.pk)
            comment.body = body
            comment.edited_at = timezone.now()
            comment.save(update_fields=["body", "edited_at"])
            append_change(locked, "comment.updated", "comment", str(comment.pk), {}, request.principal)
            append_audit(locked, "comment.updated", "comment", str(comment.pk), {}, request.principal)
        return _response(request, comment_json(comment))
    if request.method == "DELETE":
        with transaction.atomic():
            locked = Project.objects.select_for_update().get(pk=project.pk)
            comment = LeadComment.objects.select_for_update().get(pk=comment.pk)
            comment.deleted_at = timezone.now()
            comment.save(update_fields=["deleted_at"])
            append_change(
                locked,
                "comment.deleted",
                "comment",
                str(comment.pk),
                {},
                request.principal,
                tombstone=True,
            )
            append_audit(locked, "comment.deleted", "comment", str(comment.pk), {}, request.principal)
        return _response(request, None, 204)
    return _error(request, "method_not_allowed", "PATCH or DELETE required", 405)


@authenticated
@endpoint
def interested(request, project_id):
    project = _project(request, project_id, scope="interest:read")
    if request.method != "GET":
        return _error(request, "method_not_allowed", "GET required", 405)
    query = project.leads.filter(status=Lead.Status.ACTIVE)
    by = request.GET.get("interested_by", "me")
    if by == "me":
        query = query.filter(interests__user=request.principal.user)
    elif by == "any":
        query = query.filter(
            interests__user__project_memberships__project_id=project.pk
        )
    else:
        if not by.startswith("user:"):
            raise ValueError("interested_by must be me, any, or user:UUID")
        try:
            user_id = uuid.UUID(by[5:])
        except ValueError:
            raise ValueError("invalid user id")
        if not project.memberships.filter(user_id=user_id).exists():
            raise Http404
        query = query.filter(interests__user_id=user_id)
    query = query.distinct().order_by("-updated_at")
    limit = min(max(int(request.GET.get("limit", "50")), 1), 100)
    return _response(
        request,
        {
            "items": [
                lead_json(lead, request.principal, include_interest=by == "any")
                for lead in query[:limit]
            ],
            "next_cursor": "",
        },
    )


@authenticated
@endpoint
def trash(request, project_id):
    project = _project(request, project_id, scope="leads:read")
    if request.method != "GET":
        return _error(request, "method_not_allowed", "GET required", 405)
    # Bounded like ``interested``.  Unbounded, a mass-trash makes this endpoint a
    # context-blowup on the next agent run.
    rows, next_cursor = _keyset_page(
        project.leads.filter(status=Lead.Status.TRASHED),
        request,
        "updated_at",
        default_limit=100,
    )
    return _response(
        request,
        {
            "items": [lead_json(lead, request.principal) for lead in rows],
            "next_cursor": next_cursor,
        },
    )


@authenticated
@endpoint
def restore(request, project_id, lead_id):
    # Restore undoes a human decision, so it needs the destructive scope and an
    # If-Match: an agent must have read the current row before reversing it.
    project, lead = _lead_for(
        request, project_id, lead_id, "leads:destroy", ProjectMembership.Role.VIEWER
    )
    if request.method != "POST":
        return _error(request, "method_not_allowed", "POST required", 405)
    if_match = request.headers.get("If-Match", "")
    if not if_match:
        return _error(request, "if_match_required", "If-Match is required to restore a lead.", 409)
    throttled = _spend_writes(request, destructive=True)
    if throttled:
        return throttled
    try:
        lead = restore_lead(lead, actor=request.principal, if_match=if_match)
    except LeadRevisionConflict:
        return _error(request, "stale_write", "Lead changed since it was read.", 409)
    return _response(request, lead_json(lead, request.principal), headers={"ETag": lead.etag})


@authenticated
@endpoint
def lead_batch(request, project_id):
    """Apply one action to up to 100 leads in one atomic project mutation."""
    project = _project(request, project_id, role=ProjectMembership.Role.VIEWER, scope="leads:write")
    if request.method != "POST":
        return _error(request, "method_not_allowed", "POST required", 405)
    data, error = _data(request)
    if error:
        return error
    lead_ids = data.get("lead_ids", data.get("ids"))
    if not isinstance(lead_ids, list):
        raise ValueError("lead_ids must be an array")
    action = str(data.get("action", "")).strip().lower()
    # The action lives in the body, so the destructive scope is checked here,
    # after parsing.  ``batch_lead_mutation`` re-checks under its own lock.
    destructive = action in {"trash", "restore"}
    if destructive:
        _require(request.principal, project, scope="leads:destroy")
    throttled = _spend_writes(request, len(lead_ids) or 1, destructive=destructive)
    if throttled:
        return throttled
    updated = batch_lead_mutation(
        project,
        actor=request.principal,
        leads=lead_ids,
        action=action,
        comment=data.get("comment", ""),
    )
    return _response(
        request,
        {"action": action, "items": [lead_json(lead, request.principal) for lead in updated]},
    )
