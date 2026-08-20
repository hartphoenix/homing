"""Transactional source-plan review state transitions.

The server records an agent's bounded assertion about its installation.  It
does not inspect, store, or infer anything about local source files.
"""

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.db import IntegrityError, transaction
from django.utils import timezone

from projects.models import Project, SourcePlanReview
from .authorization import as_principal, assert_viewer
from .mutations import append_audit


MAX_PROMPT_REVISION = 2**31 - 1


class SourcePlanRevisionConflict(Exception):
    """The caller reported/resolved a revision other than the current one."""

    def __init__(self, current_revision):
        self.current_revision = current_revision
        super().__init__("The project prompt has changed.")


class SourcePlanReviewStale(Exception):
    """The open review has not observed the revision being resolved."""

    def __init__(self, observed_revision):
        self.observed_revision = observed_revision
        super().__init__("The source-plan review must be refreshed before resolution.")


def validate_prompt_revision(value):
    """Accept only a JSON integer in PositiveIntegerField's portable range."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("prompt_revision must be an integer")
    if value < 0 or value > MAX_PROMPT_REVISION:
        raise ValueError("prompt_revision is out of range")
    return value


def _principal(*, actor=None, user=None, agent_token=None):
    principal = as_principal(actor if actor is not None else user)
    # ``agent_token`` is useful to service callers that keep the token
    # separately; API callers normally pass a Principal and need no override.
    if agent_token is not None and principal.token is not agent_token:
        from .authorization import Principal

        principal = Principal(principal.user, agent_token)
    return principal


def _assert_source_access(project, principal):
    return assert_viewer(project, principal, scope="runs:write")


def _event_payload(review):
    # Keep event and audit payloads free of prompts, names, URLs, and agent text.
    return {
        "status": review.status,
        "observed_prompt_revision": review.observed_prompt_revision,
        "resolved_prompt_revision": review.resolved_prompt_revision,
    }


def open_source_plan_review(
    project,
    *,
    prompt_revision,
    actor=None,
    user=None,
    agent_token=None,
    request_id="",
):
    """Open or refresh one user's review, returning the durable review row.

    Locking the project serializes transitions for a project on databases that
    support row locks.  The conditional unique constraint remains the final
    guard against duplicate open rows under a concurrent insert.
    """
    revision = validate_prompt_revision(prompt_revision)
    principal = _principal(actor=actor, user=user, agent_token=agent_token)
    _assert_source_access(project, principal)
    token = principal.token
    user_obj = principal.user
    with transaction.atomic():
        locked_project = Project.objects.select_for_update().get(pk=project.pk)
        if locked_project.prompt_revision != revision:
            raise SourcePlanRevisionConflict(locked_project.prompt_revision)
        review = (
            SourcePlanReview.objects.select_for_update()
            .filter(project=locked_project, user=user_obj, status=SourcePlanReview.Status.OPEN)
            .first()
        )
        now = timezone.now()
        if review is not None:
            review.observed_prompt_revision = revision
            review.reporting_agent_token = token
            review.last_reported_at = now
            review.save(update_fields=["observed_prompt_revision", "reporting_agent_token", "last_reported_at"])
            review._created = False
            return review

        try:
            with transaction.atomic():
                review = SourcePlanReview.objects.create(
                    project=locked_project,
                    user=user_obj,
                    status=SourcePlanReview.Status.OPEN,
                    observed_prompt_revision=revision,
                    reporting_agent_token=token,
                    last_reported_at=now,
                )
        except IntegrityError:
            # This savepoint path is primarily for backends without effective
            # SELECT FOR UPDATE semantics (notably SQLite test databases).
            review = (
                SourcePlanReview.objects.select_for_update()
                .get(project=locked_project, user=user_obj, status=SourcePlanReview.Status.OPEN)
            )
            review.observed_prompt_revision = revision
            review.reporting_agent_token = token
            review.last_reported_at = now
            review.save(update_fields=["observed_prompt_revision", "reporting_agent_token", "last_reported_at"])
            review._created = False
            return review

        payload = _event_payload(review)
        append_audit(
            locked_project,
            "source_plan_review.opened",
            "source_plan_review",
            str(review.pk),
            payload,
            principal,
            request_id=request_id,
        )
        review._created = True
        return review


def report_source_plan_review(project, **kwargs):
    """Alias using the API's report terminology."""
    return open_source_plan_review(project, **kwargs)


def resolve_source_plan_review(
    project,
    review_id,
    *,
    prompt_revision,
    actor=None,
    user=None,
    agent_token=None,
    request_id="",
):
    """Resolve a review as an audited assertion by its owning user."""
    revision = validate_prompt_revision(prompt_revision)
    principal = _principal(actor=actor, user=user, agent_token=agent_token)
    _assert_source_access(project, principal)
    token = principal.token
    with transaction.atomic():
        locked_project = Project.objects.select_for_update().get(pk=project.pk)
        try:
            review = SourcePlanReview.objects.select_for_update().get(pk=review_id, project=locked_project)
        except (SourcePlanReview.DoesNotExist, ValueError):
            raise Http404
        if review.user_id != principal.user.pk:
            # The API deliberately turns this into 404 so review IDs cannot be
            # used to discover another collaborator's machine state.
            raise Http404
        if locked_project.prompt_revision != revision:
            raise SourcePlanRevisionConflict(locked_project.prompt_revision)
        if review.observed_prompt_revision != revision:
            raise SourcePlanReviewStale(review.observed_prompt_revision)
        if review.status == SourcePlanReview.Status.RESOLVED:
            if review.resolved_prompt_revision == revision:
                review._changed = False
                return review
            raise PermissionDenied("This review is already resolved.")

        now = timezone.now()
        review.status = SourcePlanReview.Status.RESOLVED
        review.resolved_prompt_revision = revision
        review.resolving_agent_token = token
        review.resolved_at = now
        review.save(update_fields=["status", "resolved_prompt_revision", "resolving_agent_token", "resolved_at"])
        payload = _event_payload(review)
        append_audit(
            locked_project,
            "source_plan_review.resolved",
            "source_plan_review",
            str(review.pk),
            payload,
            principal,
            request_id=request_id,
        )
        review._changed = True
        return review


def list_source_plan_reviews(user, *, token=None, status=SourcePlanReview.Status.OPEN, limit=100):
    """Return bounded, newest-first reviews visible to this principal."""
    principal = _principal(user=user, agent_token=token)
    if "projects:read" not in (set(principal.token.scopes or []) if principal.token else {"projects:read"}):
        raise PermissionDenied("Token lacks the required scope")
    queryset = SourcePlanReview.objects.filter(
        user=principal.user,
        status=status,
        project__status=Project.Status.ACTIVE,
        project__memberships__user=principal.user,
    )
    if principal.token and principal.token.project_ids:
        queryset = queryset.filter(project_id__in=[str(value) for value in principal.token.project_ids])
    return list(queryset.select_related("project").order_by("-last_reported_at", "-opened_at", "-id")[: max(1, min(int(limit), 100))])
