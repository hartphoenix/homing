"""Transactional mutations shared by HTML and API handlers."""
import uuid

from django.db import transaction
from django.utils import timezone
from projects.models import AuditEvent, Lead, LeadComment, LeadInterest, PromptRevision, Project, ProjectChange, ProjectMembership
from .authorization import as_principal, assert_editor, assert_owner, assert_viewer

class PromptRevisionConflict(Exception):
    def __init__(self, current_revision):
        self.current_revision = current_revision
        super().__init__(f"Prompt revision {current_revision} is current")


class LeadRevisionConflict(Exception):
    """Raised when an ``if_match`` guard does not match the stored revision."""

    def __init__(self, current_revision):
        self.current_revision = current_revision
        super().__init__(f"Lead revision {current_revision} is current")


class FinalOwnerError(Exception):
    """Raised when a membership change would leave a project ownerless."""


def _actor_user(actor):
    return actor.user if hasattr(actor, "user") else actor


def _actor_summary(actor):
    """Identify who acted, without copying anything the actor typed."""
    user = _actor_user(actor)
    token = getattr(actor, "token", None)
    return {
        "actor_id": str(user.pk) if user is not None else None,
        "actor_kind": "agent" if token else "user",
    }


@transaction.atomic
def remove_project_member(project, *, member_user_id, actor):
    """Remove one membership while preserving at least one project owner."""
    assert_owner(project, actor)
    locked = Project.objects.select_for_update().get(pk=project.pk)
    membership = locked.memberships.select_for_update().get(user_id=member_user_id)
    if (
        membership.role == ProjectMembership.Role.OWNER
        and locked.memberships.filter(role=ProjectMembership.Role.OWNER).count() == 1
    ):
        raise FinalOwnerError("A project must retain an owner.")

    payload = {"user_id": str(membership.user_id), "role": membership.role}
    append_change(
        locked,
        "membership.removed",
        "membership",
        str(membership.user_id),
        payload,
        actor,
        tombstone=True,
    )
    append_audit(
        locked,
        "membership.removed",
        "membership",
        str(membership.user_id),
        payload,
        actor,
    )
    membership.delete()

@transaction.atomic
def update_project_prompt(project, *, editor, prompt, criteria, expected_revision=None):
    assert_editor(project, editor)
    locked = Project.objects.select_for_update().get(pk=project.pk)
    if expected_revision is not None and locked.prompt_revision != expected_revision:
        raise PromptRevisionConflict(locked.prompt_revision)
    next_revision = locked.prompt_revision + 1
    locked.prompt = prompt
    locked.criteria = criteria or {}
    locked.prompt_revision = next_revision
    locked.save(update_fields=["prompt", "criteria", "prompt_revision", "updated_at"])
    revision = PromptRevision.objects.create(project=locked, revision=next_revision, prompt=prompt, criteria=criteria or {}, editor=editor.user if hasattr(editor, "user") else editor)
    append_change(locked, "prompt.updated", "project", str(locked.pk), {"revision": next_revision}, editor)
    append_audit(locked, "prompt.updated", "project", str(locked.pk), {"revision": next_revision}, editor)
    return revision

@transaction.atomic
def set_interest(lead, *, user, interested=True):
    principal = as_principal(user)
    assert_viewer(lead.project, principal, scope="interest:write")
    user_obj = principal.user
    if interested:
        obj, created = LeadInterest.objects.get_or_create(lead=lead, user=user_obj)
        # Interest is a first-class per-user change and must be visible to
        # agents through the same durable project cursor as lead changes.
        if created:
            append_change(
                lead.project,
                "interest.set",
                "lead",
                str(lead.pk),
                {"user_id": str(user_obj.pk), "interested": True},
                principal,
            )
            append_audit(
                lead.project,
                "interest.set",
                "lead",
                str(lead.pk),
                {"user_id": str(user_obj.pk), "interested": True},
                principal,
            )
        return obj
    deleted, _ = LeadInterest.objects.filter(lead=lead, user=user_obj).delete()
    if deleted:
        append_change(
            lead.project,
            "interest.set",
            "lead",
            str(lead.pk),
            {"user_id": str(user_obj.pk), "interested": False},
            principal,
        )
        append_audit(
            lead.project,
            "interest.set",
            "lead",
            str(lead.pk),
            {"user_id": str(user_obj.pk), "interested": False},
            principal,
        )
    return None

@transaction.atomic
def trash_lead(lead, *, actor, comment=None, reason=None):
    """Move a lead to shared trash and optionally append a comment.

    ``reason`` is accepted as a compatibility alias for older clients.  New
    callers should use ``comment``; an empty comment is valid and does not
    require a second confirmation in either the API or browser flow.
    """
    assert_editor(lead.project, actor, scope="leads:destroy")
    if comment is None:
        comment = reason
    body = str(comment or "").strip()[:10000]
    project = Project.objects.select_for_update().get(pk=lead.project_id)
    lead = Lead.objects.select_for_update().get(pk=lead.pk)
    lead.status = Lead.Status.TRASHED
    lead.trashed_by = actor.user if hasattr(actor, "user") else actor
    lead.trashed_at = timezone.now()
    lead.revision += 1
    lead.save(update_fields=["status", "trashed_by", "trashed_at", "revision", "updated_at"])
    comment_obj = None
    if body:
        comment_obj = LeadComment.objects.create(
            lead=lead,
            author=actor.user if hasattr(actor, "user") else actor,
            body=body,
        )
        append_change(
            project,
            "comment.created",
            "comment",
            str(comment_obj.pk),
            {"lead_id": str(lead.pk), "from_trash": True},
            actor,
        )
    append_change(
        project,
        "lead.trashed",
        "lead",
        str(lead.pk),
        {"comment_id": str(comment_obj.pk) if comment_obj else None},
        actor,
        tombstone=True,
    )
    append_audit(
        project,
        "lead.trashed",
        "lead",
        str(lead.pk),
        {"comment_id": str(comment_obj.pk) if comment_obj else None},
        actor,
    )
    return lead

@transaction.atomic
def restore_lead(lead, *, actor, if_match=None):
    """Undo a trashing.  Restore is the undo of a human decision, so it needs
    ``leads:destroy`` and — for any credential-bearing actor — an ``if_match``
    guard, and it records who did it."""
    assert_editor(lead.project, actor, scope="leads:destroy")
    if if_match is None and getattr(actor, "token", None) is not None:
        raise ValueError("if_match is required to restore a lead")
    project = Project.objects.select_for_update().get(pk=lead.project_id)
    lead = Lead.objects.select_for_update().get(pk=lead.pk)
    if if_match is not None and str(if_match).strip('"') != str(lead.revision):
        raise LeadRevisionConflict(lead.revision)
    lead.status = Lead.Status.ACTIVE
    lead.trashed_by = None
    lead.trashed_at = None
    lead.revision += 1
    lead.save(update_fields=["status", "trashed_by", "trashed_at", "revision", "updated_at"])
    summary = _actor_summary(actor)
    append_change(project, "lead.restored", "lead", str(lead.pk), summary, actor)
    append_audit(project, "lead.restored", "lead", str(lead.pk), summary, actor)
    return lead


@transaction.atomic
def batch_lead_mutation(project, *, actor, leads, action, comment=""):
    """Apply one shared action to a validated set of project leads atomically.

    The queryset is constrained to ``project`` before locking, so IDs from a
    different project can never be mutated accidentally.  Authorization is
    checked once for the project and every individual mutation runs under the
    same transaction, preserving change-feed/audit consistency.
    """
    assert_editor(project, actor, scope="leads:write")
    if action not in {"trash", "restore", "interested", "uninterested"}:
        raise ValueError("action must be trash, restore, interested, or uninterested")
    if action in {"trash", "restore"}:
        assert_editor(project, actor, scope="leads:destroy")
    try:
        lead_ids = list(dict.fromkeys(str(uuid.UUID(str(value))) for value in leads))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("lead_ids must contain UUIDs")
    if not lead_ids or len(lead_ids) > 100:
        raise ValueError("lead_ids must contain 1 to 100 leads")
    locked_project = Project.objects.select_for_update().get(pk=project.pk)
    selected = list(
        Lead.objects.select_for_update().filter(project=locked_project, pk__in=lead_ids)
    )
    if len(selected) != len(lead_ids):
        raise ValueError("Every lead must belong to the current project")
    # Preserve caller order for deterministic responses/change logs.
    by_id = {str(lead.pk): lead for lead in selected}
    selected = [by_id[value] for value in lead_ids]
    updated = []
    for lead in selected:
        if action == "trash":
            body = str(comment or "").strip()[:10000]
            lead.status = Lead.Status.TRASHED
            lead.trashed_by = actor.user if hasattr(actor, "user") else actor
            lead.trashed_at = timezone.now()
            lead.revision += 1
            lead.save(update_fields=["status", "trashed_by", "trashed_at", "revision", "updated_at"])
            comment_obj = None
            if body:
                comment_obj = LeadComment.objects.create(
                    lead=lead,
                    author=actor.user if hasattr(actor, "user") else actor,
                    body=body,
                )
                append_change(
                    locked_project,
                    "comment.created",
                    "comment",
                    str(comment_obj.pk),
                    {"lead_id": str(lead.pk), "from_trash": True},
                    actor,
                )
            payload = {"comment_id": str(comment_obj.pk) if comment_obj else None}
            append_change(locked_project, "lead.trashed", "lead", str(lead.pk), payload, actor, tombstone=True)
            append_audit(locked_project, "lead.trashed", "lead", str(lead.pk), payload, actor)
        elif action == "restore":
            lead.status = Lead.Status.ACTIVE
            lead.trashed_by = None
            lead.trashed_at = None
            lead.revision += 1
            lead.save(update_fields=["status", "trashed_by", "trashed_at", "revision", "updated_at"])
            payload = _actor_summary(actor)
            append_change(locked_project, "lead.restored", "lead", str(lead.pk), payload, actor)
            append_audit(locked_project, "lead.restored", "lead", str(lead.pk), payload, actor)
        else:
            user_obj = actor.user if hasattr(actor, "user") else actor
            if action == "interested":
                interest, created = LeadInterest.objects.get_or_create(lead=lead, user=user_obj)
                if created:
                    payload = {"user_id": str(user_obj.pk), "interested": True}
                    append_change(locked_project, "interest.set", "lead", str(lead.pk), payload, actor)
                    append_audit(locked_project, "interest.set", "lead", str(lead.pk), payload, actor)
            else:
                deleted, _ = LeadInterest.objects.filter(lead=lead, user=user_obj).delete()
                if deleted:
                    payload = {"user_id": str(user_obj.pk), "interested": False}
                    append_change(locked_project, "interest.set", "lead", str(lead.pk), payload, actor)
                    append_audit(locked_project, "interest.set", "lead", str(lead.pk), payload, actor)
        updated.append(lead)
    return updated


def append_audit(project, action, object_type, object_id, summary=None, actor=None, *, request_id=""):
    """Write a bounded audit row in the caller's transaction."""
    if actor is not None and hasattr(actor, "user"):
        user, token, actor_kind = actor.user, actor.token, "agent" if actor.token else "user"
    else:
        user, token, actor_kind = actor, None, "user"
    return AuditEvent.objects.create(
        project=project,
        actor=user,
        actor_kind=actor_kind,
        token_id=token.pk if token else None,
        request_id=str(request_id or "")[:100],
        action=action,
        object_type=object_type,
        object_id=str(object_id or "")[:100],
        summary=summary or {},
    )

@transaction.atomic
def append_change(project, event_type, object_type, object_id, payload, actor=None, *, tombstone=False):
    """Append a monotonic project cursor under a serialized project-row lock."""
    # Lock here as a defense-in-depth guarantee.  A caller that forgot to
    # lock the project must not be able to allocate duplicate/out-of-order
    # sequences under concurrent writes.  The nested lock is safe for callers
    # already inside their own atomic block.
    locked = Project.objects.select_for_update().get(pk=project.pk)
    if actor is not None and hasattr(actor, "user"):
        user, token = actor.user, actor.token
        actor_kind = "agent" if token else "user"
    else:
        user, token, actor_kind = actor, None, "user"
    locked.latest_change_sequence += 1
    locked.save(update_fields=["latest_change_sequence", "updated_at"])
    return ProjectChange.objects.create(project=locked, sequence=locked.latest_change_sequence, event_type=event_type, object_type=object_type, object_id=object_id, payload=payload or {}, tombstone=tombstone, actor=user, actor_kind=actor_kind, token_id=token.pk if token else None)
