"""Transactional mutations shared by HTML and API handlers."""
from django.db import transaction
from django.utils import timezone
from projects.models import Lead, LeadInterest, PromptRevision, Project, ProjectChange
from .authorization import assert_editor, assert_viewer

class PromptRevisionConflict(Exception):
    def __init__(self, current_revision):
        self.current_revision = current_revision
        super().__init__(f"Prompt revision {current_revision} is current")

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
    return revision

@transaction.atomic
def set_interest(lead, *, user, interested=True):
    assert_viewer(lead.project, user, scope="interest:write")
    if interested:
        obj, _ = LeadInterest.objects.get_or_create(lead=lead, user=user)
        return obj
    LeadInterest.objects.filter(lead=lead, user=user).delete()
    return None

@transaction.atomic
def trash_lead(lead, *, actor, reason):
    assert_editor(lead.project, actor, scope="leads:write")
    if not reason or not reason.strip():
        raise ValueError("A reason is required when trashing a lead")
    lead = Lead.objects.select_for_update().get(pk=lead.pk)
    lead.status = Lead.Status.TRASHED
    lead.trash_reason = reason.strip()[:1000]
    lead.trashed_by = actor.user if hasattr(actor, "user") else actor
    lead.trashed_at = timezone.now()
    lead.revision += 1
    lead.save(update_fields=["status", "trash_reason", "trashed_by", "trashed_at", "revision", "updated_at"])
    append_change(lead.project, "lead.trashed", "lead", str(lead.pk), {"reason": lead.trash_reason}, actor, tombstone=True)
    return lead

@transaction.atomic
def restore_lead(lead, *, actor):
    assert_editor(lead.project, actor, scope="leads:write")
    lead = Lead.objects.select_for_update().get(pk=lead.pk)
    lead.status = Lead.Status.ACTIVE
    lead.trash_reason = ""
    lead.trashed_by = None
    lead.trashed_at = None
    lead.revision += 1
    lead.save(update_fields=["status", "trash_reason", "trashed_by", "trashed_at", "revision", "updated_at"])
    append_change(lead.project, "lead.restored", "lead", str(lead.pk), {}, actor)
    return lead

def append_change(project, event_type, object_type, object_id, payload, actor=None, *, tombstone=False):
    """Append a monotonic project cursor; caller should hold the project row lock."""
    if actor is not None and hasattr(actor, "user"):
        user, token = actor.user, actor.token
        actor_kind = "agent" if token else "user"
    else:
        user, token, actor_kind = actor, None, "user"
    project.latest_change_sequence += 1
    project.save(update_fields=["latest_change_sequence", "updated_at"])
    return ProjectChange.objects.create(project=project, sequence=project.latest_change_sequence, event_type=event_type, object_type=object_type, object_id=object_id, payload=payload or {}, tombstone=tombstone, actor=user, actor_kind=actor_kind, token_id=token.pk if token else None)
