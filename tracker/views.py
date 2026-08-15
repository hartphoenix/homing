"""Human-facing, permission-aware tracker views.

All writes are POST-only and pass through the same authorization/mutation
services used by the API.  The views intentionally keep presentation shaping
here; models remain the source of truth and are never exposed as private
profile blobs to project members.
"""

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import patch_vary_headers
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts.models import AgentToken, Profile, SavedPrompt
from accounts.services.tokens import create_agent_token, revoke_agent_token
from projects.models import (
    Lead,
    LeadComment,
    LeadInterest,
    AuditEvent,
    Project,
    ProjectInvitation,
    ProjectMembership,
)
from projects.services.authorization import ROLE_RANK, SCOPES, authorize_project
from projects.services.mutations import (
    PromptRevisionConflict,
    append_change,
    restore_lead,
    set_interest,
    trash_lead,
    update_project_prompt,
)

from .forms import (
    AgentTokenForm,
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
from .agent_guidance import build_agent_prompt, build_skill_markdown


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
    rank = ROLE_RANK[membership.role]
    return {
        "membership": membership,
        "can_manage_project": membership.role == ProjectMembership.Role.OWNER,
        "can_edit_prompt": rank >= ROLE_RANK[ProjectMembership.Role.EDITOR],
        "can_edit_leads": rank >= ROLE_RANK[ProjectMembership.Role.EDITOR],
        "can_comment": rank >= ROLE_RANK[ProjectMembership.Role.VIEWER],
    }


def _profile(user):
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile


def _agent_api_base_url(request):
    public_base = getattr(settings, "PUBLIC_BASE_URL", "")
    return f"{public_base}/api/v1" if public_base else request.build_absolute_uri("/api/v1").rstrip("/")


def _lead_view(lead, user):
    """Attach only safe display projections used by the thin templates."""
    lead.url = lead.canonical_url
    lead.is_trashed = lead.status == Lead.Status.TRASHED
    lead.is_interested = LeadInterest.objects.filter(lead=lead, user=user).exists()
    lead.comment_count = lead.comments.filter(deleted_at__isnull=True).count()
    names = []
    for interest in lead.interests.select_related("user__profile"):
        profile = getattr(interest.user, "profile", None)
        names.append(
            type(
                "Member",
                (),
                {"display_name": (profile.display_name if profile else "") or interest.user.email},
            )()
        )
    lead.interested_members = names
    unknowns = []
    for field, label in (
        ("availability", "availability"),
        ("location", "location"),
        ("price_display", "price"),
        ("summary", "summary"),
    ):
        if not getattr(lead, field):
            unknowns.append(label)
    if lead.is_trashed and lead.trash_reason:
        unknowns.append(f"trash reason: {lead.trash_reason}")
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

    def get_success_url(self):
        return _safe_next(self.request, self.get_redirect_url(), reverse("tracker:project-list"))


class TrackerLogoutView(LogoutView):
    next_page = "login"


def register(request):
    if request.user.is_authenticated:
        return redirect("tracker:project-list")
    if not getattr(settings, "ALLOW_PUBLIC_SIGNUP", False):
        return render(
            request, "tracker/register.html", {"form": RegisterForm(), "disabled": True}, status=403
        )
    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        messages.success(request, "Your account is ready.")
        return redirect("tracker:project-list")
    return render(request, "tracker/register.html", {"form": form})


@login_required
def project_list(request):
    projects = list(
        Project.objects.filter(memberships__user=request.user)
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
        project.role = roles.get(str(project.pk), "viewer")
    return render(request, "tracker/project_list.html", {"projects": projects})


@login_required
def project_create(request):
    form = ProjectForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            project = form.save(commit=False)
            project.creator = request.user
            project.save()
            ProjectMembership.objects.create(
                project=project, user=request.user, role=ProjectMembership.Role.OWNER
            )
        messages.success(request, "Project created.")
        return redirect("tracker:project-detail", project.slug)
    return render(request, "tracker/project_form.html", {"form": form, "title": "Create project"})


@login_required
def project_detail(request, slug):
    project = _project(slug, request.user)
    flags = _project_flags(project, request.user)
    project.active_count = project.leads.filter(status=Lead.Status.ACTIVE).count()
    project.trash_count = project.leads.filter(status=Lead.Status.TRASHED).count()
    project.interested_count = LeadInterest.objects.filter(
        lead__project=project, user=request.user
    ).count()
    project.member_count = project.memberships.count()
    latest = project.prompt_revisions.select_related("editor__profile").first()
    project.prompt_updated_at = latest.created_at if latest else None
    project.prompt_editor = (
        (
            getattr(latest.editor, "profile", None).display_name
            if latest and hasattr(latest.editor, "profile")
            else None
        )
        if latest
        else None
    )
    return render(request, "tracker/project_detail.html", {"project": project, **flags})


@login_required
def project_edit(request, slug):
    project = _project(slug, request.user, ProjectMembership.Role.OWNER)
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
    project = _project(slug, request.user, ProjectMembership.Role.OWNER)
    members = []
    for membership in project.memberships.select_related("user__profile"):
        profile = getattr(membership.user, "profile", None)
        members.append(
            {
                "display_name": (profile.display_name if profile else "") or membership.user.email,
                "email": membership.user.email,
                "role": membership.role,
            }
        )
    invitations = [
        {"email": invite.invited_email, "role": invite.role, "expires_at": invite.expires_at}
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
            **_project_flags(project, request.user),
        },
    )


@login_required
def prompt_edit(request, slug):
    project = _project(slug, request.user, ProjectMembership.Role.EDITOR)
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
    is_trash = params.get("status") == "trash"
    qs = project.leads.select_related("creator").prefetch_related("interests__user__profile")
    qs = qs.filter(status=Lead.Status.TRASHED if is_trash else Lead.Status.ACTIVE)
    q = params.get("q", "").strip()
    date_confidence = params.get("date_confidence", "")
    housing_type = params.get("housing_type", "")
    interested_by = params.get("interested_by", "")
    if q:
        qs = qs.filter(
            Q(title__icontains=q)
            | Q(summary__icontains=q)
            | Q(location__icontains=q)
            | Q(source__icontains=q)
        )
    if date_confidence:
        qs = qs.filter(date_confidence=date_confidence)
    if housing_type:
        qs = qs.filter(housing_type=housing_type)
    if interested_by == "me":
        qs = qs.filter(interests__user=request.user)
    return (
        qs.distinct(),
        {
            "q": q,
            "date_confidence": date_confidence,
            "housing_type": housing_type,
            "interested_by": interested_by,
        },
        is_trash,
    )


@login_required
def lead_list(request, slug):
    project = _project(slug, request.user)
    leads, filters, is_trash = _filtered_leads(project, request)
    leads = [_lead_view(lead, request.user) for lead in leads]
    flags = _project_flags(project, request.user)
    project.active_count = project.leads.filter(status=Lead.Status.ACTIVE).count()
    project.trash_count = project.leads.filter(status=Lead.Status.TRASHED).count()
    return render(
        request,
        "tracker/lead_list.html",
        {
            "project": project,
            "leads": leads,
            "filters": filters,
            "is_trash": is_trash,
            "date_confidence_choices": Lead.DateConfidence.choices,
            "housing_choices": Lead.HousingType.choices,
            **flags,
        },
    )


@login_required
def lead_create(request, slug):
    project = _project(slug, request.user, ProjectMembership.Role.EDITOR)
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
    project = _project(slug, request.user, ProjectMembership.Role.EDITOR)
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
    lead = get_object_or_404(Lead, pk=lead_id, project=project)
    lead = _lead_view(lead, request.user)
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
            comment.author_id == request.user.pk or membership.role == ProjectMembership.Role.OWNER
        )
        comments.append(comment)
    flags = _project_flags(project, request.user)
    return render(
        request,
        "tracker/lead_detail.html",
        {"project": project, "lead": lead, "comments": comments, **flags},
    )


@login_required
def lead_trash(request, slug, lead_id):
    project = _project(slug, request.user, ProjectMembership.Role.EDITOR)
    lead = get_object_or_404(Lead, pk=lead_id, project=project)
    form = TrashLeadForm(request.POST or None, initial={"reason": lead.trash_reason})
    if request.method == "POST" and form.is_valid():
        trash_lead(lead, actor=request.user, reason=form.cleaned_data["reason"])
        messages.success(request, "Lead moved to trash. It can be restored.")
        return redirect("tracker:lead-list", project.slug)
    return render(
        request,
        "tracker/prompt_form.html",
        {
            "project": project,
            "form": form,
            "title": "Move lead to trash",
            **_project_flags(project, request.user),
        },
    )


@login_required
@require_POST
def lead_restore(request, slug, lead_id):
    project = _project(slug, request.user, ProjectMembership.Role.EDITOR)
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
def lead_interest(request, slug, lead_id):
    project = _project(slug, request.user)
    lead = get_object_or_404(Lead, pk=lead_id, project=project)
    interested = request.POST.get("interested", "true").lower() not in {"0", "false", "no", "off"}
    set_interest(lead, user=request.user, interested=interested)
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


@login_required
def member_invite(request, slug):
    project = _project(slug, request.user, ProjectMembership.Role.OWNER)
    form = InvitationForm(request.POST or None)
    invitation_url = None
    if request.method == "POST" and form.is_valid():
        raw_token = secrets.token_urlsafe(32)
        ProjectInvitation.objects.create(
            project=project,
            invited_email=form.cleaned_data["email"],
            role=form.cleaned_data["role"],
            inviter=request.user,
            token_digest=hashlib.sha256(raw_token.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(days=7),
        )
        invitation_url = request.build_absolute_uri(
            reverse("tracker:invitation-accept", args=[raw_token])
        )
        messages.success(request, "Invitation created. Send the link to the member.")
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
        "inviter_name": invitation.inviter.email if invitation else "A project owner",
        "role": invitation.role if invitation else "viewer",
        "email": invitation.invited_email if invitation else "",
    }
    if not invitation or not invitation.is_pending:
        display["error"] = "This invitation is expired, revoked, or already used."
        return render(
            request, "tracker/invitation_accept.html", {"invitation": display}, status=410
        )
    if not request.user.is_authenticated:
        display["requires_login"] = True
        return render(request, "tracker/invitation_accept.html", {"invitation": display})
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
            elif not ProjectMembership.objects.filter(
                project=locked.project, user=locked.inviter, role=ProjectMembership.Role.OWNER
            ).exists():
                display["error"] = "The project no longer has an active owner who can share it."
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


@login_required
def agent_setup(request):
    form = AgentTokenForm(request.POST or None)
    raw = None
    if request.method == "POST" and form.is_valid():
        token, raw = create_agent_token(
            user=request.user,
            name=form.cleaned_data["name"],
            scopes=SCOPES,
            project_ids=[],
            expires_at=form.cleaned_data["expires_at"],
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
        form = AgentTokenForm()
    api_base_url = _agent_api_base_url(request)
    response = render(
        request,
        "tracker/agent_setup.html",
        {
            "form": form,
            "new_token": raw,
            "agent_prompt": build_agent_prompt(api_base_url),
            "tokens": AgentToken.objects.filter(user=request.user),
        },
    )
    response["Cache-Control"] = "private, no-store"
    response["Pragma"] = "no-cache"
    return response


@login_required
def agent_skill_download(request):
    api_base_url = _agent_api_base_url(request)
    response = HttpResponse(build_skill_markdown(api_base_url), content_type="text/markdown; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="SKILL.md"'
    response["Cache-Control"] = "private, no-store"
    response["Pragma"] = "no-cache"
    patch_vary_headers(response, ("Cookie",))
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
