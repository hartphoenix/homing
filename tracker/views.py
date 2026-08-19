"""Human-facing, permission-aware tracker views.

All writes are POST-only and pass through the same authorization/mutation
services used by the API.  The views intentionally keep presentation shaping
here; models remain the source of truth and are never exposed as private
profile blobs to project members.
"""

import hashlib
import secrets
import uuid
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts.managers import normalize_email
from accounts.models import AgentLink, AgentToken, Profile, SavedPrompt, normalize_user_code
from accounts.services import throttle
from accounts.services.email import send_invitation_email
from accounts.services.tokens import create_agent_token, default_agent_scopes, revoke_agent_token
from projects.models import (
    Lead,
    LeadComment,
    AuditEvent,
    Project,
    ProjectInvitation,
    ProjectMembership,
    SearchRun,
    unique_project_slug,
)
from projects.services.authorization import authorize_project
from projects.services.mutations import (
    FinalOwnerError,
    PromptRevisionConflict,
    SelfRemovalError,
    append_change,
    batch_lead_mutation,
    remove_project_member,
    restore_lead,
    set_interest,
    trash_lead,
    trash_project,
    update_project_prompt,
)

from .forms import (
    CommentForm,
    EmailAuthenticationForm,
    InvitationForm,
    LeadForm,
    ProfileForm,
    ProjectForm,
    PromptForm,
    RegisterForm,
    SavedPromptForm,
    TrashLeadForm,
)
from .agent_guidance import build_agent_prompt
from .source_plan_reviews import open_review_count


def _safe_next(request, candidate, fallback):
    if candidate and url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return fallback


def _project(slug, user, role=ProjectMembership.Role.VIEWER):
    project = get_object_or_404(Project, slug=slug)
    authorize_project(project, user, minimum_role=role)
    return project


def _project_flags(project, user):
    membership = authorize_project(project, user)
    return {
        "membership": membership,
        "can_manage_project": membership.role == ProjectMembership.Role.OWNER,
        "can_view_settings": True,
        "can_edit_project": True,
        # All collaborators share project-content authority. Owner remains an
        # administrative safeguard only for role changes and orphan prevention.
        "can_edit_prompt": True,
        "can_edit_leads": True,
        "can_comment": True,
    }


def _profile(user):
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile


def _agent_api_base_url(request):
    public_base = getattr(settings, "PUBLIC_BASE_URL", "")
    return f"{public_base}/api/v1" if public_base else request.build_absolute_uri("/api/v1").rstrip("/")


def _lead_view(lead, user, *, internal_url=None):
    """Attach only safe display projections used by the thin templates."""
    lead.url = lead.canonical_url
    lead.is_trashed = lead.status == Lead.Status.TRASHED
    interests = list(lead.interests.all())
    lead.is_interested = any(interest.user_id == user.pk for interest in interests)
    lead.interest_count = len(interests)
    lead.comment_count = lead.comments.filter(deleted_at__isnull=True).count()
    names = []
    for interest in interests:
        profile = getattr(interest.user, "profile", None)
        names.append(
            type(
                "Member",
                (),
                {"display_name": (profile.display_name if profile else "") or interest.user.email},
            )()
        )
    lead.interested_members = names
    trashed_by = getattr(lead, "trashed_by", None)
    trashed_profile = getattr(trashed_by, "profile", None) if trashed_by else None
    lead.trashed_by_name = (
        (trashed_profile.display_name if trashed_profile else "") or trashed_by.email
        if trashed_by
        else ""
    )
    lead.internal_url = internal_url
    unknowns = []
    for field, label in (
        ("availability", "availability"),
        ("location", "location"),
        ("price_display", "price"),
        ("summary", "summary"),
    ):
        if not getattr(lead, field):
            unknowns.append(label)
    lead.unknowns = unknowns
    return lead


class TrackerLoginView(LoginView):
    template_name = "tracker/login.html"
    authentication_form = EmailAuthenticationForm
    redirect_authenticated_user = True

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["public_signup_enabled"] = settings.ALLOW_PUBLIC_SIGNUP
        return context

    def dispatch(self, request, *args, **kwargs):
        # Do not let a login form (and its CSRF token) be restored from a
        # shared/browser cache after the session or token has changed.
        response = super().dispatch(request, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        response["Pragma"] = "no-cache"
        return response

    def get_success_url(self):
        return _safe_next(self.request, self.get_redirect_url(), reverse("tracker:project-list"))


class TrackerLogoutView(LogoutView):
    next_page = "login"


def register(request):
    if request.user.is_authenticated:
        return redirect("tracker:project-list")
    invite_token = request.POST.get("invite") or request.GET.get("invite", "")
    login_url = reverse("login")
    if invite_token:
        invitation_path = reverse("tracker:invitation-accept", args=[invite_token])
        login_url = f"{login_url}?{urlencode({'next': invitation_path})}"
    invitation = None
    invitation_error = None
    if invite_token:
        invitation = (
            ProjectInvitation.objects.select_related("project")
            .filter(token_digest=hashlib.sha256(invite_token.encode()).hexdigest())
            .first()
        )
        if not invitation or not invitation.is_pending:
            invitation = None
            invitation_error = "This invitation is expired, revoked, or already used."
    public_signup = getattr(settings, "ALLOW_PUBLIC_SIGNUP", False)
    if not public_signup and not invitation:
        return render(
            request,
            "tracker/register.html",
            {
                "form": RegisterForm(initial={"email": invitation.invited_email} if invitation else None),
                "disabled": True,
                "invite_token": invite_token,
                "invitation_error": invitation_error,
                "login_url": login_url,
            },
            status=403,
        )
    initial = {"email": invitation.invited_email} if invitation else None
    form = RegisterForm(
        request.POST or None,
        initial=initial,
        locked_email=invitation.invited_email if invitation else None,
    )
    if request.method == "POST" and form.is_valid():
        if invitation and normalize_email(form.cleaned_data["email"]) != invitation.invited_email:
            form.add_error("email", "Use the invited email address to create this account.")
        elif invitation_error:
            form.add_error(None, invitation_error)
        else:
            user = form.save()
            login(request, user)
            messages.success(request, "Your account is ready. Review the invitation to join the project.")
            if invitation:
                return redirect("tracker:invitation-accept", invite_token)
            return redirect("tracker:project-list")
    return render(
        request,
        "tracker/register.html",
        {
            "form": form,
            "invite_token": invite_token,
            "invitation": invitation,
            "invitation_error": invitation_error,
            "login_url": login_url,
        },
    )


@login_required
def project_list(request):
    projects = list(
        Project.objects.filter(
            memberships__user=request.user, status=Project.Status.ACTIVE
        )
        .annotate(
            active_count=Count("leads", filter=Q(leads__status=Lead.Status.ACTIVE), distinct=True),
            member_count=Count("memberships", distinct=True),
        )
        .order_by("name")
    )
    roles = {
        str(row.project_id): row.role
        for row in ProjectMembership.objects.filter(project__in=projects, user=request.user)
    }
    for project in projects:
        project.role = (
            "owner"
            if roles.get(str(project.pk)) == ProjectMembership.Role.OWNER
            else "collaborator"
        )
    return render(request, "tracker/project_list.html", {"projects": projects})


@login_required
def project_create(request):
    form = ProjectForm(request.POST or None, creator_email=request.user.email)
    if request.method == "POST" and form.is_valid():
        # The availability check in unique_project_slug is intentionally
        # human-readable. A short retry closes the check-then-insert race when
        # two people create the same project name concurrently.
        for attempt in range(3):
            try:
                with transaction.atomic():
                    project = form.save(commit=False)
                    project.pk = uuid.uuid4()
                    project.creator = request.user
                    project.slug = unique_project_slug(project.name)
                    project.save()
                    ProjectMembership.objects.create(
                        project=project, user=request.user, role=ProjectMembership.Role.OWNER
                    )
                    append_change(project, "project.created", "project", str(project.pk), {}, request.user)
                    if form.cleaned_data["prompt"]:
                        update_project_prompt(
                            project,
                            editor=request.user,
                            prompt=form.cleaned_data["prompt"],
                            criteria={},
                            expected_revision=0,
                        )
                    for email in form.cleaned_data["invite_emails"]:
                        _send_project_invitation(project, request.user, email, request=request)
                break
            except IntegrityError:
                if attempt == 2:
                    raise
        messages.success(request, "Project created.")
        return redirect("tracker:lead-list", project.slug)
    return render(request, "tracker/project_form.html", {"form": form, "title": "Create project"})


@login_required
def project_detail(request, slug):
    project = _project(slug, request.user)
    return redirect("tracker:lead-list", project.slug)


@login_required
def project_edit(request, slug):
    project = _project(slug, request.user)
    form = ProjectForm(request.POST or None, instance=project)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Project details saved.")
        return redirect("tracker:project-settings", project.slug)
    return render(
        request,
        "tracker/project_form.html",
        {"form": form, "title": "Edit project", "project": project},
    )


@login_required
def project_settings(request, slug):
    project = _project(slug, request.user)
    flags = _project_flags(project, request.user)
    owner_count = project.memberships.filter(role=ProjectMembership.Role.OWNER).count()
    members = []
    for membership in project.memberships.select_related("user__profile"):
        profile = getattr(membership.user, "profile", None)
        members.append(
            {
                "display_name": (profile.display_name if profile else "") or membership.user.email,
                "email": membership.user.email,
                "user_id": membership.user_id,
                "is_current_user": membership.user_id == request.user.pk,
                "role": (
                    "owner"
                    if membership.role == ProjectMembership.Role.OWNER
                    else "collaborator"
                ),
                "can_remove": flags["can_manage_project"]
                and membership.user_id != request.user.pk
                and not (
                    membership.role == ProjectMembership.Role.OWNER and owner_count == 1
                ),
            }
        )
    invitations = [
        {"email": invite.invited_email, "role": "collaborator", "expires_at": invite.expires_at}
        for invite in project.invitations.filter(accepted_at__isnull=True, revoked_at__isnull=True)
        if invite.expires_at > timezone.now()
    ]
    return render(
        request,
        "tracker/project_settings.html",
        {
            "project": project,
            "members": members,
            "invitations": invitations,
            **flags,
        },
    )


@login_required
@require_POST
def member_remove(request, slug, user_id):
    project = _project(slug, request.user, ProjectMembership.Role.OWNER)
    membership = get_object_or_404(
        project.memberships.select_related("user__profile"), user_id=user_id
    )
    profile = getattr(membership.user, "profile", None)
    display_name = (profile.display_name if profile else "") or membership.user.email
    try:
        remove_project_member(project, member_user_id=user_id, actor=request.user)
    except FinalOwnerError:
        messages.error(request, "A project must retain at least one owner.")
    except SelfRemovalError:
        messages.error(request, "You cannot remove yourself from a project.")
    else:
        messages.success(request, f"{display_name} was removed from the project.")
    return redirect("tracker:project-settings", project.slug)


@login_required
@require_POST
def project_delete(request, slug):
    """Move a project to the recoverable trash state."""
    project = _project(slug, request.user, ProjectMembership.Role.OWNER)
    trash_project(project, actor=request.user)
    messages.success(request, f"{project.name} was moved to trash.")
    return redirect("tracker:project-list")


@login_required
def prompt_edit(request, slug):
    project = _project(slug, request.user)
    flags = _project_flags(project, request.user)
    form = PromptForm(request.POST or None, project=project)
    if request.method == "POST" and form.is_valid():
        expected = request.POST.get("expected_revision")
        try:
            expected_revision = int(expected) if expected not in (None, "") else None
        except (TypeError, ValueError):
            form.add_error(
                "prompt", "The revision marker is invalid. Reload this prompt and try again."
            )
            return render(
                request,
                "tracker/prompt_form.html",
                {"project": project, "form": form, "title": "Search prompt", "is_prompt_form": True, **flags},
                status=409,
            )
        try:
            update_project_prompt(
                project,
                editor=request.user,
                prompt=form.cleaned_data["prompt"],
                criteria=form.cleaned_data["criteria"],
                expected_revision=expected_revision,
            )
        except PromptRevisionConflict as exc:
            form.add_error(
                None,
                f"Someone saved revision {exc.current_revision} while you were editing. Your draft is still here; reload to compare.",
            )
            return render(
                request,
                "tracker/prompt_form.html",
                {"project": project, "form": form, "title": "Search prompt", "is_prompt_form": True, **flags},
                status=409,
            )
        messages.success(request, "Search prompt saved.")
        return redirect("tracker:project-detail", project.slug)
    return render(request, "tracker/prompt_form.html", {"project": project, "form": form, "title": "Search prompt", "is_prompt_form": True, **flags})


def _filtered_leads(project, request):
    params = request.GET
    status = params.get("status", "active")
    if status not in {"active", "trash"}:
        status = "active"
    is_trash = status == "trash"
    qs = project.leads.select_related("creator", "trashed_by__profile").prefetch_related("interests__user__profile")
    qs = qs.filter(status=Lead.Status.TRASHED if is_trash else Lead.Status.ACTIVE)
    q = params.get("q", "").strip()
    interest_scope = params.get("interest_scope", "").strip().lower()
    # Older links used interested_by=me.  Continue accepting them while the
    # broader scope control uses an explicit, self-describing parameter.
    if not interest_scope and params.get("interested_by") == "me":
        interest_scope = "me"
    if interest_scope not in {"all", "me", "anyone"}:
        interest_scope = "all"
    sort = params.get("sort", "updated").strip().lower()
    if sort not in {"updated", "newest", "oldest", "interest"}:
        sort = "updated"
    if q:
        qs = qs.filter(
            Q(title__icontains=q)
            | Q(summary__icontains=q)
            | Q(location__icontains=q)
            | Q(source__icontains=q)
        )
    if interest_scope == "me":
        qs = qs.filter(interests__user=request.user)
    elif interest_scope == "anyone":
        qs = qs.filter(interests__isnull=False)
    if sort == "newest":
        qs = qs.order_by("-created_at", "-pk")
    elif sort == "oldest":
        qs = qs.order_by("created_at", "pk")
    elif sort == "interest":
        qs = qs.annotate(interest_total=Count("interests", distinct=True)).order_by(
            "-interest_total", "-updated_at", "-pk"
        )
    else:
        # The model's existing default is recently updated, so retain that
        # behavior as the new universal sort default.
        qs = qs.order_by("-updated_at", "-pk")
    return (
        qs.distinct(),
        {
            "q": q,
            "interest_scope": interest_scope,
            # Keep this projection for callers that still inspect the legacy
            # checkbox state while links migrate to interest_scope.
            "interested_by": "me" if interest_scope == "me" else "",
            "status": status,
            "sort": sort,
            # List is the compact, scan-friendly default.  An explicit view
            # query parameter remains authoritative so links stay shareable.
            "view": params.get("view", "list") if params.get("view") in {"cards", "list"} else "list",
        },
        is_trash,
    )


@login_required
def lead_list(request, slug):
    project = _project(slug, request.user)
    leads, filters, is_trash = _filtered_leads(project, request)
    leads = [
        _lead_view(
            lead,
            request.user,
            internal_url=request.build_absolute_uri(
                reverse("tracker:lead-detail", args=[project.slug, lead.pk])
            ),
        )
        for lead in leads
    ]
    flags = _project_flags(project, request.user)
    project.active_count = project.leads.filter(status=Lead.Status.ACTIVE).count()
    project.trash_count = project.leads.filter(status=Lead.Status.TRASHED).count()
    list_params = request.GET.copy()
    list_params["view"] = "list"
    cards_params = request.GET.copy()
    cards_params["view"] = "cards"
    clear_params = request.GET.copy()
    for key in (
        "q",
        "interest_scope",
        "interested_by",
        "date_confidence",
        "housing_type",
        "status",
        "sort",
        "view",
    ):
        clear_params.pop(key, None)
    base_url = reverse("tracker:lead-list", args=[project.slug])
    return render(
        request,
        "tracker/lead_list.html",
        {
            "project": project,
            "leads": leads,
            "filters": filters,
            "is_trash": is_trash,
            "view_mode": filters["view"],
            "cards_url": f"{base_url}?{cards_params.urlencode()}",
            "list_url": f"{base_url}?{list_params.urlencode()}",
            "clear_url": f"{base_url}?{clear_params.urlencode()}" if clear_params else base_url,
            **flags,
        },
    )


@login_required
def lead_create(request, slug):
    project = _project(slug, request.user)
    form = LeadForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            lead = form.save(commit=False)
            lead.project = project
            lead.creator = request.user
            lead.save()
            append_change(
                project,
                "lead.created",
                "lead",
                str(lead.pk),
                {"revision": lead.revision},
                request.user,
            )
        messages.success(request, "Lead added.")
        return redirect("tracker:lead-detail", project.slug, lead.pk)
    return render(
        request,
        "tracker/project_form.html",
        {
            "project": project,
            "form": form,
            "title": "Add lead",
            **_project_flags(project, request.user),
        },
    )


def _render_lead_edit(request, project, lead, form, *, status=200):
    return render(
        request,
        "tracker/project_form.html",
        {
            "project": project,
            "lead": lead,
            "form": form,
            "title": "Edit lead",
            **_project_flags(project, request.user),
        },
        status=status,
    )


@login_required
def lead_edit(request, slug, lead_id):
    project = _project(slug, request.user)
    lead = get_object_or_404(Lead, pk=lead_id, project=project)
    form = LeadForm(request.POST or None, instance=lead)
    if request.method == "POST" and form.is_valid():
        expected = form.cleaned_data.get("expected_revision")
        if_match = request.headers.get("If-Match", "").strip().strip('"')
        if if_match.isdigit():
            expected = int(if_match)
        with transaction.atomic():
            locked = Lead.objects.select_for_update().get(pk=lead.pk)
            if expected is not None and expected != locked.revision:
                form.add_error(
                    None,
                    "This lead changed while you were editing. Your draft is still here; reload to compare.",
                )
                return _render_lead_edit(request, project, locked, form, status=409)
            for field in LeadForm.Meta.fields:
                if field != "expected_revision":
                    setattr(locked, field, form.cleaned_data.get(field))
            locked.revision += 1
            locked.save()
            append_change(
                project,
                "lead.updated",
                "lead",
                str(locked.pk),
                {"revision": locked.revision},
                request.user,
            )
        messages.success(request, "Lead updated.")
        return redirect("tracker:lead-detail", project.slug, lead.pk)
    return _render_lead_edit(request, project, lead, form)


@login_required
def lead_detail(request, slug, lead_id):
    project = _project(slug, request.user)
    lead = get_object_or_404(
        Lead.objects.select_related("trashed_by__profile").prefetch_related(
            "interests__user__profile"
        ),
        pk=lead_id,
        project=project,
    )
    lead = _lead_view(
        lead,
        request.user,
        internal_url=request.build_absolute_uri(
            reverse("tracker:lead-detail", args=[project.slug, lead.pk])
        ),
    )
    lead.facts = {
        "Price": lead.price_display,
        "Location": lead.location,
        "Availability": lead.availability,
        "Housing": lead.get_housing_type_display(),
        "Date confidence": lead.get_date_confidence_display(),
        "Parks": lead.park_notes,
        "Source": lead.source,
    }
    comments = []
    membership = authorize_project(project, request.user)
    for comment in lead.comments.filter(deleted_at__isnull=True).select_related("author__profile"):
        profile = getattr(comment.author, "profile", None)
        comment.author.display_name = (
            profile.display_name if profile else ""
        ) or comment.author.email
        comment.is_edited = bool(comment.edited_at)
        comment.can_edit = (
            comment.author_id == request.user.pk
            or membership.role == ProjectMembership.Role.OWNER
        )
        comments.append(comment)
    flags = _project_flags(project, request.user)
    return render(
        request,
        "tracker/lead_detail.html",
        {"project": project, "lead": lead, "comments": comments, **flags},
    )


def _trash_for_web(lead, *, actor, comment=""):
    """Trash a lead and optionally append a normal attributed comment."""
    return trash_lead(lead, actor=actor, comment=(comment or "").strip())


@login_required
def lead_trash(request, slug, lead_id):
    project = _project(slug, request.user)
    lead = get_object_or_404(Lead, pk=lead_id, project=project)
    form = TrashLeadForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        _trash_for_web(lead, actor=request.user, comment=form.cleaned_data.get("comment", ""))
        messages.success(request, "Lead moved to trash. It can be restored.")
        return redirect(
            _safe_next(
                request,
                request.POST.get("next"),
                reverse("tracker:lead-list", args=[project.slug]),
            )
        )
    return render(
        request,
        "tracker/lead_trash.html",
        {
            "project": project,
            "lead": lead,
            "form": form,
            "next": request.GET.get("next", request.POST.get("next", "")),
            **_project_flags(project, request.user),
        },
    )


@login_required
@require_POST
def lead_restore(request, slug, lead_id):
    project = _project(slug, request.user)
    lead = get_object_or_404(Lead, pk=lead_id, project=project)
    restore_lead(lead, actor=request.user)
    messages.success(request, "Lead restored.")
    return redirect(
        _safe_next(
            request, request.POST.get("next"), reverse("tracker:lead-list", args=[project.slug])
        )
    )


@login_required
@require_POST
def lead_batch(request, slug):
    """Apply a single, authorized action to a checked set of project leads."""
    project = _project(slug, request.user)
    raw_ids = request.POST.getlist("lead_ids")
    action = request.POST.get("action", "").strip().lower()
    allowed_actions = {"interest", "uninterest", "trash", "restore"}
    if not raw_ids or action not in allowed_actions:
        messages.error(request, "Choose at least one lead and an action.")
        return redirect(
            _safe_next(request, request.POST.get("next"), reverse("tracker:lead-list", args=[project.slug]))
        )

    # Query by project and compare the complete submitted set. This prevents
    # an attacker from smuggling a lead from another project into a batch.
    wanted = {value for value in raw_ids if value}
    leads = list(Lead.objects.filter(project=project, pk__in=wanted).order_by("pk"))
    if len(leads) != len(wanted):
        messages.error(request, "One or more selected leads are not in this project.")
        return redirect(
            _safe_next(request, request.POST.get("next"), reverse("tracker:lead-list", args=[project.slug]))
        )
    comment = request.POST.get("comment", "")
    service_action = {
        "interest": "interested",
        "uninterest": "uninterested",
        "trash": "trash",
        "restore": "restore",
    }[action]
    with transaction.atomic():
        batch_lead_mutation(
            project,
            actor=request.user,
            leads=[lead.pk for lead in leads],
            action=service_action,
            comment=comment,
        )
    labels = {
        "interest": "marked interested",
        "uninterest": "interest removed from",
        "trash": "moved to trash",
        "restore": "restored",
    }
    messages.success(request, f"{len(leads)} lead(s) {labels[action]}.")
    return redirect(
        _safe_next(request, request.POST.get("next"), reverse("tracker:lead-list", args=[project.slug]))
    )


@login_required
@require_POST
def lead_interest(request, slug, lead_id):
    project = _project(slug, request.user)
    lead = get_object_or_404(Lead, pk=lead_id, project=project)
    interested = request.POST.get("interested", "true").lower() not in {"0", "false", "no", "off"}
    set_interest(lead, user=request.user, interested=interested)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or "application/json" in request.headers.get("Accept", ""):
        interests = list(lead.interests.select_related("user__profile"))
        members = []
        for interest in interests:
            profile = getattr(interest.user, "profile", None)
            members.append((profile.display_name if profile else "") or interest.user.email)
        return JsonResponse(
            {
                "is_interested": any(interest.user_id == request.user.pk for interest in interests),
                "interest_count": len(interests),
                "interested_members": members,
            }
        )
    return redirect(
        _safe_next(
            request, request.POST.get("next"), reverse("tracker:lead-list", args=[project.slug])
        )
    )


@login_required
@require_POST
def comment_create(request, slug, lead_id):
    project = _project(slug, request.user)
    lead = get_object_or_404(Lead, pk=lead_id, project=project)
    form = CommentForm(request.POST)
    if form.is_valid():
        comment = form.save(commit=False)
        comment.lead, comment.author = lead, request.user
        comment.save()
        append_change(project, "comment.created", "comment", str(comment.pk), {}, request.user)
        messages.success(request, "Comment added.")
        return redirect("tracker:lead-detail", project.slug, lead.pk)
    messages.error(request, "Add a comment before saving.")
    return redirect("tracker:lead-detail", project.slug, lead.pk)


@login_required
def comment_edit(request, slug, lead_id, comment_id):
    project = _project(slug, request.user)
    lead = get_object_or_404(Lead, pk=lead_id, project=project)
    comment = get_object_or_404(LeadComment, pk=comment_id, lead=lead, deleted_at__isnull=True)
    membership = authorize_project(project, request.user)
    if comment.author_id != request.user.pk and membership.role != ProjectMembership.Role.OWNER:
        return HttpResponse(status=403)
    form = CommentForm(request.POST or None, instance=comment)
    if request.method == "POST" and form.is_valid():
        form.save(commit=False)
        comment.edited_at = timezone.now()
        comment.save(update_fields=["body", "edited_at"])
        append_change(project, "comment.updated", "comment", str(comment.pk), {}, request.user)
        messages.success(request, "Comment updated.")
        return redirect("tracker:lead-detail", project.slug, lead.pk)
    return render(
        request,
        "tracker/prompt_form.html",
        {
            "project": project,
            "form": form,
            "title": "Edit comment",
            **_project_flags(project, request.user),
        },
    )


@login_required
@require_POST
def comment_delete(request, slug, lead_id, comment_id):
    project = _project(slug, request.user)
    lead = get_object_or_404(Lead, pk=lead_id, project=project)
    comment = get_object_or_404(LeadComment, pk=comment_id, lead=lead, deleted_at__isnull=True)
    membership = authorize_project(project, request.user)
    if comment.author_id != request.user.pk and membership.role != ProjectMembership.Role.OWNER:
        return HttpResponse(status=403)
    comment.deleted_at = timezone.now()
    comment.save(update_fields=["deleted_at"])
    append_change(
        project, "comment.deleted", "comment", str(comment.pk), {}, request.user, tombstone=True
    )
    messages.success(request, "Comment deleted.")
    return redirect("tracker:lead-detail", project.slug, lead.pk)


def _send_project_invitation(project, inviter, email, *, request=None):
    """Create and deliver one invitation using the normal invitation flow."""
    raw_token = secrets.token_urlsafe(32)
    invitation = ProjectInvitation.objects.create(
        project=project,
        invited_email=email,
        role=ProjectMembership.Role.VIEWER,
        inviter=inviter,
        token_digest=hashlib.sha256(raw_token.encode()).hexdigest(),
        expires_at=timezone.now() + timedelta(days=7),
    )
    return invitation, send_invitation_email(
        invitation=invitation, raw_token=raw_token, request=request
    )


@login_required
def member_invite(request, slug):
    project = _project(slug, request.user, ProjectMembership.Role.VIEWER)
    form = InvitationForm(request.POST or None)
    invitation_url = None
    if request.method == "POST" and form.is_valid():
        _, invitation_url = _send_project_invitation(
            project, request.user, form.cleaned_data["email"], request=request
        )
        messages.success(request, "Invitation created and sent to the member.")
    return render(
        request,
        "tracker/member_invite.html",
        {
            "project": project,
            "form": form,
            "invitation_url": invitation_url,
            **_project_flags(project, request.user),
        },
    )


def invitation_accept(request, token):
    digest = hashlib.sha256(token.encode()).hexdigest()
    invitation = (
        ProjectInvitation.objects.select_related("project", "inviter")
        .filter(token_digest=digest)
        .first()
    )
    display = {
        "project_name": invitation.project.name if invitation else "this project",
        "inviter_name": (
            (
                getattr(invitation.inviter, "profile", None).display_name
                if getattr(invitation.inviter, "profile", None)
                else ""
            )
            or invitation.inviter.email
            if invitation
            else "A project collaborator"
        ),
        "role": "collaborator",
        "email": invitation.invited_email if invitation else "",
    }
    if (
        not invitation
        or not invitation.is_pending
        or invitation.project.status == Project.Status.TRASHED
    ):
        display["error"] = "This invitation is expired, revoked, or already used."
        return render(
            request, "tracker/invitation_accept.html", {"invitation": display}, status=410
        )
    if not request.user.is_authenticated:
        query = urlencode({"invite": token})
        return redirect(f"{reverse('tracker:register')}?{query}")
    if request.user.email != invitation.invited_email:
        display["error"] = f"Sign in as {invitation.invited_email} to accept this invitation."
        return render(
            request, "tracker/invitation_accept.html", {"invitation": display}, status=403
        )
    if request.method == "POST":
        if request.POST.get("action") == "reject":
            invitation.revoked_at = timezone.now()
            invitation.save(update_fields=["revoked_at"])
            messages.info(request, "Invitation declined.")
            return redirect("tracker:project-list")
        with transaction.atomic():
            locked = (
                ProjectInvitation.objects.select_for_update()
                .select_related("project")
                .get(pk=invitation.pk)
            )
            if not locked.is_pending:
                display["error"] = "This invitation is no longer available."
            elif locked.project.status == Project.Status.TRASHED:
                display["error"] = "This project is no longer available."
            elif not locked.inviter.is_active or not ProjectMembership.objects.filter(
                project=locked.project, user=locked.inviter
            ).exists():
                display["error"] = "The collaborator who invited you no longer belongs to this project."
            else:
                ProjectMembership.objects.get_or_create(
                    project=locked.project, user=request.user, defaults={"role": locked.role}
                )
                locked.accepted_at = timezone.now()
                locked.save(update_fields=["accepted_at"])
                messages.success(request, f"You joined {locked.project.name}.")
                return redirect("tracker:project-detail", locked.project.slug)
        return render(
            request, "tracker/invitation_accept.html", {"invitation": display}, status=409
        )
    display["requires_login"] = False
    return render(request, "tracker/invitation_accept.html", {"invitation": display})


@login_required
def profile(request):
    profile_obj = _profile(request.user)
    form = ProfileForm(request.POST or None, instance=profile_obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Profile saved.")
        return redirect("tracker:profile")
    tokens = []
    for token in AgentToken.objects.filter(user=request.user):
        token.scope_summary = ", ".join(token.scopes or []) or "No scopes"
        tokens.append(token)
    return render(request, "tracker/profile.html", {"form": form, "tokens": tokens})


@login_required
def token_create(request):
    return redirect("tracker:agent-setup")


# A pause that never ends is a disconnect nobody remembers making, so pausing
# has a date on it and the page says what that date is.
AGENT_PAUSE_DAYS = 14
# Two missed cycles plus a margin for a laptop that woke up late.  Below this
# the page says "running"; above it, "hasn't checked in".
AGENT_SILENCE_FACTOR = 2
AGENT_SILENCE_GRACE = timedelta(minutes=45)
# Wrong pairing codes, per signed-in person.  Six characters from a 32-symbol
# alphabet is ~30 bits; five tries per quarter hour makes guessing hopeless.
LINK_CODE_MAX_ATTEMPTS = 5
LINK_CODE_WINDOW = timedelta(minutes=15)
# Pending links carry no owner until somebody decides on them, so the only
# leak-free way to resurface one on the setup page is the approver's session.
LINK_SESSION_KEY = "agent_link_id"


def _consume_link_attempt(request):
    """Charge one failed pairing-code entry; returns ``(blocked, retry_after)``.

    Its own bucket on purpose: mistyping a code must never lock a person out
    of signing in, and a failed sign-in must never stop them approving.
    """
    return throttle._consume_counters(
        [("agent-link-code", str(request.user.pk), LINK_CODE_MAX_ATTEMPTS, LINK_CODE_WINDOW)]
    )


def _live_agent_tokens(user):
    """Connections that can still be used, newest first."""
    return list(
        AgentToken.objects.filter(
            user=user, revoked_at__isnull=True, expires_at__gt=timezone.now()
        )
    )


def _cadence_text(minutes):
    if not minutes:
        return "Whenever you ask"
    if minutes < 60:
        return f"Every {minutes} minutes"
    if minutes % 1440 == 0:
        days = minutes // 1440
        return "Every day" if days == 1 else f"Every {days} days"
    if minutes % 60 == 0:
        hours = minutes // 60
        return "Every hour" if hours == 1 else f"Every {hours} hours"
    return f"Every {minutes} minutes"


def _count(counts, key):
    try:
        return max(0, int(counts.get(key) or 0))
    except (AttributeError, TypeError, ValueError):
        return 0


def _check_outcome(run):
    """One plain sentence about a finished check.

    A source that could not be reached is never reported as "nothing new" —
    a silent zero is a lie to somebody looking for a home.
    """
    counts = run.result_counts if isinstance(run.result_counts, dict) else {}
    if run.status == SearchRun.Status.COMPLETED:
        found = []
        if _count(counts, "created"):
            found.append(f"{_count(counts, 'created')} new")
        if _count(counts, "updated"):
            found.append(f"{_count(counts, 'updated')} updated")
        line = ", ".join(found) if found else "nothing new"
        unreachable = _count(counts, "sources_blocked")
        if unreachable:
            plural = "" if unreachable == 1 else "s"
            line += f" · couldn't reach {unreachable} place{plural} to look"
        return line
    if run.status == SearchRun.Status.FAILED:
        return "didn't finish"
    if run.status == SearchRun.Status.CANCELLED:
        return "stopped"
    return "still going"


def _agent_checks(user, limit=5):
    runs = list(
        SearchRun.objects.filter(user=user, agent_token__isnull=False).select_related("project")[
            :limit
        ]
    )
    for run in runs:
        run.happened_at = run.completed_at or run.started_at or run.created_at
        run.outcome = _check_outcome(run)
    return runs


def _session_link(request):
    """The pairing request this person last looked at, if it is still relevant."""
    raw = request.session.get(LINK_SESSION_KEY)
    link = None
    if raw:
        try:
            link = AgentLink.objects.filter(pk=uuid.UUID(str(raw))).first()
        except (TypeError, ValueError):
            link = None
    if link is None or link.status in (AgentLink.Status.DENIED, AgentLink.Status.EXPIRED):
        request.session.pop(LINK_SESSION_KEY, None)
        return None
    if link.status == AgentLink.Status.PENDING and link.is_expired:
        request.session.pop(LINK_SESSION_KEY, None)
        return None
    if link.status == AgentLink.Status.CONSUMED:
        request.session.pop(LINK_SESSION_KEY, None)
        return None
    return link


def _disconnect(request, token_id):
    try:
        parsed = uuid.UUID(str(token_id))
    except (TypeError, ValueError):
        return False
    token = AgentToken.objects.filter(pk=parsed, user=request.user).first()
    if token is None:
        return False
    revoke_agent_token(token)
    AuditEvent.objects.create(
        actor=request.user,
        actor_kind="user",
        token_id=token.pk,
        action="agent_token.revoked",
        object_type="agent_token",
        object_id=str(token.pk),
        summary={"name": token.name},
    )
    return True


@login_required
def agent_setup(request):
    """One page, four states: not connected, asked to connect, running, silent."""
    profile_obj = _profile(request.user)
    new_key = None
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "pause":
            profile_obj.agent_paused_until = timezone.now() + timedelta(days=AGENT_PAUSE_DAYS)
            profile_obj.save(update_fields=["agent_paused_until", "updated_at"])
            messages.success(request, "Paused. Nothing new will be added until you resume.")
            return redirect("tracker:agent-setup")
        if action == "resume":
            profile_obj.agent_paused_until = None
            profile_obj.save(update_fields=["agent_paused_until", "updated_at"])
            messages.success(request, "Resumed. Your assistant will pick up at its next check.")
            return redirect("tracker:agent-setup")
        if action == "disconnect":
            if _disconnect(request, request.POST.get("token_id")):
                messages.success(request, "Disconnected. Everything it already found stays.")
            else:
                messages.error(request, "That connection is already gone.")
            return redirect("tracker:agent-setup")
        if action == "create-key":
            name = (request.POST.get("name") or "").strip()[:120] or "My search assistant"
            # A key a person carries by hand has been seen by whatever showed
            # it to them, so it is recorded as exposed and never gets the
            # destructive scope.
            token, new_key = create_agent_token(
                user=request.user,
                name=name,
                scopes=default_agent_scopes(),
                project_ids=[],
                exposed_to_chat=True,
            )
            AuditEvent.objects.create(
                actor=request.user,
                actor_kind="user",
                token_id=token.pk,
                action="agent_token.created",
                object_type="agent_token",
                object_id=str(token.pk),
                summary={"name": token.name, "expires_at": token.expires_at.isoformat()},
            )

    now = timezone.now()
    link = _session_link(request)
    agents = _live_agent_tokens(request.user)
    agent = agents[0] if agents else None
    checks = _agent_checks(request.user)
    last_check = checks[0] if checks else None
    cadence = agent.expected_cadence_minutes if agent else None
    next_check = None
    silent = False
    if agent and cadence:
        anchor = last_check.happened_at if last_check else agent.created_at
        next_check = anchor + timedelta(minutes=cadence)
        silent = now > anchor + timedelta(minutes=cadence * AGENT_SILENCE_FACTOR) + AGENT_SILENCE_GRACE
    if link is not None and link.is_open:
        state = "pending"
    elif agent is None:
        state = "new"
    elif silent:
        state = "silent"
    else:
        state = "running"
    paused_until = profile_obj.agent_paused_until
    if paused_until and paused_until <= now:
        paused_until = None
    response = render(
        request,
        "tracker/agent_setup.html",
        {
            "state": state,
            "agent": agent,
            "agents": agents,
            "tokens": AgentToken.objects.filter(user=request.user),
            "pending_link": link if state == "pending" else None,
            "approved_link": link if link is not None and not link.is_open else None,
            "agent_prompt": build_agent_prompt(_agent_api_base_url(request)),
            "source_plan_review_count": open_review_count(request.user),
            "source_review_prompt": build_agent_prompt(
                _agent_api_base_url(request), repair=True
            ),
            "new_key": new_key,
            "paused_until": paused_until,
            "cadence_text": _cadence_text(cadence),
            "checks": checks,
            "last_check": last_check,
            "next_check": next_check,
            "searching": list(
                Project.objects.filter(
                    memberships__user=request.user, status=Project.Status.ACTIVE
                )
                .order_by("name")
                .values_list("name", flat=True)[:6]
            ),
        },
    )
    response["Cache-Control"] = "private, no-store"
    response["Pragma"] = "no-cache"
    return response


@login_required
def agent_link(request):
    """Approve or deny one assistant's request to connect.

    The card names the assistant and shows the code so the person can match it
    against the screen the request came from; an unknown, expired or already
    decided code gets one plain sentence that never says which it was.
    """
    typed = request.POST.get("code") if request.method == "POST" else request.GET.get("code")
    code = normalize_user_code(typed or "")
    link = AgentLink.open_for_code(code) if code else None
    outcome = ""
    error = ""
    status_code = 200
    retry_after = 0
    if code and link is None:
        blocked, retry_after = _consume_link_attempt(request)
        if blocked:
            error = "Too many tries. Wait about fifteen minutes, then try again."
            status_code = 429
        else:
            error = (
                "That code doesn't match a request we're waiting for. Check the code your "
                "assistant is showing and try again, or ask it to start over."
            )
    elif request.method == "POST" and link is not None:
        action = request.POST.get("action", "")
        request_id = request.headers.get("X-Request-Id", "")
        if action == "approve":
            link.approve(request.user, request_id=request_id)
            request.session[LINK_SESSION_KEY] = str(link.pk)
            outcome = "approved"
        elif action == "deny":
            link.deny(request.user, request_id=request_id)
            request.session.pop(LINK_SESSION_KEY, None)
            outcome = "denied"
    if link is not None and not outcome:
        request.session[LINK_SESSION_KEY] = str(link.pk)
    response = render(
        request,
        "tracker/agent_link.html",
        {"link": link, "code": code, "outcome": outcome, "error": error},
        status=status_code,
    )
    if status_code == 429:
        response["Retry-After"] = str(max(1, int(retry_after)))
    response["Cache-Control"] = "private, no-store"
    response["Pragma"] = "no-cache"
    return response


@login_required
@require_POST
def token_revoke(request, token_id):
    token = get_object_or_404(AgentToken, pk=token_id, user=request.user)
    revoke_agent_token(token)
    AuditEvent.objects.create(
        actor=request.user,
        actor_kind="user",
        token_id=token.pk,
        action="agent_token.revoked",
        object_type="agent_token",
        object_id=str(token.pk),
        summary={"name": token.name},
    )
    messages.success(request, "Agent token revoked.")
    destination = _safe_next(
        request,
        request.POST.get("next"),
        reverse("tracker:profile"),
    )
    return redirect(destination)


@login_required
def saved_prompts(request):
    return render(
        request,
        "tracker/saved_prompts.html",
        {"prompts": SavedPrompt.objects.filter(user=request.user)},
    )


@login_required
def saved_prompt_create(request):
    form = SavedPromptForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        prompt = form.save(commit=False)
        prompt.user = request.user
        prompt.save()
        messages.success(request, "Prompt saved.")
        return redirect("tracker:saved-prompts")
    return render(request, "tracker/project_form.html", {"form": form, "title": "New saved prompt"})


@login_required
def saved_prompt_edit(request, prompt_id):
    prompt = get_object_or_404(SavedPrompt, pk=prompt_id, user=request.user)
    if request.method == "POST" and request.POST.get("action") == "delete":
        prompt.delete()
        messages.success(request, "Saved prompt deleted.")
        return redirect("tracker:saved-prompts")
    form = SavedPromptForm(request.POST or None, instance=prompt)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Prompt updated.")
        return redirect("tracker:saved-prompts")
    return render(
        request, "tracker/project_form.html", {"form": form, "title": "Edit saved prompt"}
    )
