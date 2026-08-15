"""Small, dependency-free JSON API.  All authorization is delegated to the project policy."""

import json
import secrets
import uuid
from datetime import timedelta
from functools import wraps
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import authenticate
from django.http import JsonResponse, Http404
from django.core.exceptions import PermissionDenied
from django.utils import timezone
from django.utils.text import slugify
from django.db import IntegrityError, transaction

from accounts.models import AgentToken, Profile, SavedPrompt, User
from accounts.managers import normalize_email
from accounts.services.tokens import create_agent_token, digest_token, revoke_agent_token
from projects.models import (
    Lead,
    LeadComment,
    Project,
    listing_identity_hash,
    ProjectInvitation,
    ProjectMembership,
    PromptRevision,
    SearchRun,
)
from projects.services.authorization import (
    Principal,
    SCOPES,
    assert_editor,
    assert_owner,
    authorize_project,
)
from projects.services.mutations import (
    PromptRevisionConflict,
    append_change,
    restore_lead,
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


def _error(request, code, message, status, fields=None):
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


def _principal(request):
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return Principal(user)
    authorization = request.headers.get("Authorization", "")
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


def authenticated(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        principal = _principal(request)
        if principal is None:
            return _error(request, "unauthorized", "Authentication is required.", 401)
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
    except Exception as exc:
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
    if include_interest:
        for interest in lead.interests.select_related("user__profile").all():
            profile = getattr(interest.user, "profile", None)
            names.append(
                profile.display_name or interest.user.email if profile else interest.user.email
            )
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
        "trash_reason": lead.trash_reason or None,
        "interested_users": names,
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


@_json(auth=False)
def register(request):
    if request.method != "POST":
        return _error(request, "method_not_allowed", "POST required", 405)
    if not getattr(settings, "ALLOW_PUBLIC_SIGNUP", True):
        return _error(request, "registration_disabled", "Registration is disabled.", 403)
    data, error = _data(request)
    if error:
        return error
    email = normalize_email(str(data.get("email", "")))
    password = str(data.get("password", ""))
    if len(password) < 12 or "@" not in email:
        raise ValueError("A valid email and password of at least 12 characters are required.")
    if User.objects.filter(email=email).exists():
        return _error(request, "email_exists", "Unable to register with these details.", 409)
    user = User.objects.create_user(email=email, password=password)
    Profile.objects.create(user=user, display_name=str(data.get("display_name", ""))[:120])
    return _response(request, user_json(user), 201)


@_json(auth=False)
def token_exchange(request):
    if request.method != "POST":
        return _error(request, "method_not_allowed", "POST required", 405)
    data, error = _data(request)
    if error:
        return error
    user = authenticate(
        request,
        username=normalize_email(str(data.get("email", ""))),
        password=data.get("password", ""),
    )
    if not user or not user.is_active:
        return _error(request, "unauthorized", "Invalid credentials.", 401)
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
    project_ids = data.get("project_ids") or []
    try:
        [uuid.UUID(str(v)) for v in project_ids]
    except (ValueError, TypeError):
        raise ValueError("project_ids must contain UUIDs.")
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
        project_ids=data.get("project_ids"),
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


@authenticated
@endpoint
def me(request):
    if request.method != "GET":
        return _error(request, "method_not_allowed", "GET required", 405)
    return _response(request, user_json(request.principal.user, True))


@authenticated
@endpoint
def profile(request):
    profile_obj, _ = Profile.objects.get_or_create(user=request.principal.user)
    if request.method == "GET":
        return _response(request, profile_json(profile_obj))
    if request.method != "PATCH":
        return _error(request, "method_not_allowed", "GET or PATCH required", 405)
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


def _project_list(request):
    memberships = ProjectMembership.objects.filter(user=request.principal.user).select_related(
        "project"
    )
    return {
        "items": [
            project_json(m.project, m)
            for m in memberships
            if m.project.status != Project.Status.TRASHED
        ]
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
    assert_editor(project, request.principal)
    data, error = _data(request)
    if error:
        return error
    for field in ("name", "description"):
        if field in data:
            setattr(project, field, str(data[field])[: 10000 if field == "description" else 200])
    if "status" in data:
        if data["status"] not in ("active", "trashed"):
            raise ValueError("invalid status")
        project.status = data["status"]
    project.save()
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
                    {
                        "user_id": str(m.user_id),
                        "display_name": getattr(m.user.profile, "display_name", "") or m.user.email,
                        "role": m.role,
                    }
                    for m in project.memberships.select_related("user__profile")
                ]
            },
        )
    if request.method != "PATCH":
        return _error(request, "method_not_allowed", "GET or PATCH required", 405)
    assert_owner(project, request.principal)
    data, error = _data(request)
    if error:
        return error
    try:
        member = project.memberships.get(user_id=data["user_id"])
    except (KeyError, ProjectMembership.DoesNotExist):
        raise Http404
    role = data.get("role")
    if role not in ProjectMembership.Role.values:
        raise ValueError("invalid role")
    if (
        member.role == ProjectMembership.Role.OWNER
        and role != member.role
        and project.memberships.filter(role="owner").count() == 1
    ):
        return _error(request, "final_owner", "A project must retain an owner.", 409)
    member.role = role
    member.save(update_fields=["role"])
    return _response(request, {"user_id": str(member.user_id), "role": member.role})


@authenticated
@endpoint
def invitations(request, project_id):
    project = _project(
        request, project_id, role=ProjectMembership.Role.OWNER, scope="projects:read"
    )
    if request.method != "POST":
        return _error(request, "method_not_allowed", "POST required", 405)
    data, error = _data(request)
    if error:
        return error
    email, role = normalize_email(str(data.get("email", ""))), data.get("role", "viewer")
    if "@" not in email or role not in ("editor", "viewer"):
        raise ValueError("valid email and role are required")
    raw = secrets.token_urlsafe(32)
    invitation = ProjectInvitation.objects.create(
        project=project,
        invited_email=email,
        role=role,
        inviter=request.principal.user,
        token_digest=digest_token(raw),
        expires_at=timezone.now() + timedelta(days=7),
    )
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
    with transaction.atomic():
        invitation = (
            ProjectInvitation.objects.select_for_update()
            .filter(token_digest=digest_token(invitation_token))
            .first()
        )
        if not invitation or not invitation.is_pending:
            raise Http404
        if normalize_email(request.principal.user.email) != invitation.invited_email:
            return _error(
                request,
                "invited_email_mismatch",
                "This invitation belongs to another account.",
                409,
            )
        if not invitation.project.memberships.filter(user=request.principal.user).exists():
            ProjectMembership.objects.create(
                project=invitation.project, user=request.principal.user, role=invitation.role
            )
        invitation.accepted_at = timezone.now()
        invitation.save(update_fields=["accepted_at"])
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
        return _response(request, {"items": [run_json(r) for r in project.search_runs.all()[:100]]})
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
            return _response(request, run_json(existing), 201)
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
    with transaction.atomic():
        project, run = _check_run(request, project_id, run_id)
        if run.status in ("completed", "failed"):
            return _response(request, run_json(run))
        if (
            run.claim_token != data["claim_token"]
            or not run.lease_expires_at
            or run.lease_expires_at <= timezone.now()
        ):
            return _error(request, "invalid_claim", "Claim token is invalid or expired.", 409)
        run.status = data["status"]
        run.output_cursor = str(data.get("output_cursor", ""))[:500]
        run.continuation = data.get("continuation") or {}
        run.result_counts = data.get("result_counts") or {}
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
    return _response(request, run_json(run))


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
        role=ProjectMembership.Role.VIEWER,
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
    vals.update(project=project, creator=request.principal.user)
    lead = Lead.objects.create(**vals)
    with transaction.atomic():
        locked = Project.objects.select_for_update().get(pk=project.pk)
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
    project = _project(request, project_id, role=ProjectMembership.Role.EDITOR, scope="leads:write")
    if request.method != "POST":
        return _error(request, "method_not_allowed", "POST required", 405)
    data, error = _data(request)
    if error:
        return error
    items = data.get("items")
    if not isinstance(items, list) or not items or len(items) > 100:
        raise ValueError("items must contain 1 to 100 records")
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
                    results.append({"index": index, "outcome": "updated", "lead": lead_json(lead)})
                else:
                    results.append(
                        {"index": index, "outcome": "unchanged", "lead": lead_json(lead)}
                    )
            else:
                lead = Lead.objects.create(project=project, creator=request.principal.user, **vals)
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
        request, project_id, lead_id, "leads:read" if request.method == "GET" else "leads:write"
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
        trash_lead(lead, actor=request.principal, reason=str(data.get("reason", "")))
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
    for key, value in vals.items():
        setattr(lead, key, value)
    lead.revision += 1
    lead.save()
    return _response(request, lead_json(lead), headers={"ETag": lead.etag})


@authenticated
@endpoint
def interest(request, project_id, lead_id):
    project, lead = _lead_for(request, project_id, lead_id, "interest:write")
    if request.method == "PUT":
        set_interest(lead, user=request.principal, interested=True)
        return _response(request, None, 204)
    if request.method == "DELETE":
        set_interest(lead, user=request.principal, interested=False)
        return _response(request, None, 204)
    return _error(request, "method_not_allowed", "PUT or DELETE required", 405)


def comment_json(comment):
    profile = getattr(comment.author, "profile", None)
    return {
        "id": comment.id,
        "body": comment.body,
        "author_id": str(comment.author_id),
        "author_display_name": profile.display_name or comment.author.email
        if profile
        else comment.author.email,
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
    comment = LeadComment.objects.create(lead=lead, author=request.principal.user, body=body)
    with transaction.atomic():
        locked = Project.objects.select_for_update().get(pk=project.pk)
        append_change(locked, "comment.created", "comment", str(comment.pk), {}, request.principal)
    return _response(request, comment_json(comment), 201)


@authenticated
@endpoint
def comment_detail(request, project_id, lead_id, comment_id):
    project, lead = _lead_for(request, project_id, lead_id, "comments:write")
    try:
        comment = lead.comments.get(pk=comment_id)
    except LeadComment.DoesNotExist:
        raise Http404
    owner = comment.author_id == request.principal.user.pk
    membership = project.memberships.get(user=request.principal.user)
    if not owner and membership.role != ProjectMembership.Role.OWNER:
        raise PermissionError("Only the author or project owner can moderate comments.")
    if request.method == "PATCH":
        data, error = _data(request)
        if error:
            return error
        body = str(data.get("body", ""))
        if not body.strip() or len(body) > 10000:
            raise ValueError("body must contain 1 to 10000 characters")
        comment.body = body
        comment.edited_at = timezone.now()
        comment.save(update_fields=["body", "edited_at"])
        return _response(request, comment_json(comment))
    if request.method == "DELETE":
        comment.deleted_at = timezone.now()
        comment.save(update_fields=["deleted_at"])
        return _response(request, None, 204)
    return _error(request, "method_not_allowed", "PATCH or DELETE required", 405)


@authenticated
@endpoint
def interested(request, project_id):
    project = _project(request, project_id, scope="leads:read")
    if request.method != "GET":
        return _error(request, "method_not_allowed", "GET required", 405)
    query = project.leads.filter(status=Lead.Status.ACTIVE)
    by = request.GET.get("interested_by", "me")
    if by == "me":
        query = query.filter(interests__user=request.principal.user)
    elif by == "any":
        query = query.filter(interests__isnull=False)
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
    return _response(
        request,
        {
            "items": [
                lead_json(lead, request.principal)
                for lead in project.leads.filter(status=Lead.Status.TRASHED)
            ],
            "next_cursor": "",
        },
    )


@authenticated
@endpoint
def restore(request, project_id, lead_id):
    project, lead = _lead_for(
        request, project_id, lead_id, "leads:write", ProjectMembership.Role.EDITOR
    )
    if request.method != "POST":
        return _error(request, "method_not_allowed", "POST required", 405)
    return _response(
        request, lead_json(restore_lead(lead, actor=request.principal), request.principal)
    )
