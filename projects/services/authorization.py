"""Centralized project authorization policy."""
from dataclasses import dataclass
from django.core.exceptions import PermissionDenied
from django.http import Http404
from projects.models import ProjectMembership

# "leads:destroy" covers trashing and restoring.  It is deliberately separate
# from "leads:write" so a paired agent token cannot hold it: additive writes are
# reversible, trash/restore are the two verbs that undo a human's decision.
SCOPES = frozenset({"profile:read", "projects:read", "prompts:read", "leads:read", "leads:write", "leads:destroy", "comments:read", "comments:write", "interest:read", "interest:write", "runs:write"})
ROLE_RANK = {ProjectMembership.Role.VIEWER: 10, ProjectMembership.Role.EDITOR: 20, ProjectMembership.Role.OWNER: 30}

@dataclass(frozen=True)
class Principal:
    user: object
    token: object = None

def as_principal(principal):
    if isinstance(principal, Principal):
        return principal
    if hasattr(principal, "user") and not hasattr(principal, "is_authenticated"):
        return Principal(principal.user, getattr(principal, "token", None))
    return Principal(principal)

def token_is_valid(token):
    return bool(token and token.is_valid)

def effective_scopes(principal):
    context = as_principal(principal)
    if not context.token:
        return SCOPES
    if not token_is_valid(context.token):
        return frozenset()
    return frozenset(context.token.scopes or []).intersection(SCOPES)

def project_is_restricted(project, token):
    allowed = (token.project_ids or []) if token else []
    return bool(allowed) and str(project.pk) not in {str(value) for value in allowed}

def get_membership(project, principal):
    context = as_principal(principal)
    if not getattr(context.user, "is_authenticated", False):
        return None
    return ProjectMembership.objects.filter(project=project, user=context.user).first()

def authorize_project(project, principal, *, minimum_role=ProjectMembership.Role.VIEWER, scope=None):
    """Return membership; inaccessible projects are 404 and policy failures 403."""
    context = as_principal(principal)
    membership = get_membership(project, context)
    if membership is None or project_is_restricted(project, context.token):
        raise Http404("Project not found")
    if context.token and not token_is_valid(context.token):
        raise PermissionDenied("Token is expired or revoked")
    if ROLE_RANK[membership.role] < ROLE_RANK[minimum_role]:
        raise PermissionDenied("Insufficient project role")
    if scope and scope not in effective_scopes(context):
        raise PermissionDenied("Token lacks the required scope")
    return membership

def can_access(project, principal, *, minimum_role=ProjectMembership.Role.VIEWER, scope=None):
    try:
        authorize_project(project, principal, minimum_role=minimum_role, scope=scope)
    except (Http404, PermissionDenied):
        return False
    return True

def assert_owner(project, principal):
    return authorize_project(project, principal, minimum_role=ProjectMembership.Role.OWNER)

def assert_editor(project, principal, *, scope=None):
    """Authorize a project content mutation.

    Historical callers use the ``assert_editor`` name, but Homing's current
    collaboration policy gives every project member equal content authority.
    The owner/editor/viewer role values remain for administrative compatibility
    (and for the owner safeguard), while content writes require membership only.
    """
    return authorize_project(project, principal, minimum_role=ProjectMembership.Role.VIEWER, scope=scope)


def assert_collaborator(project, principal, *, scope=None):
    """Authorize any active project collaborator for shared content."""
    return authorize_project(project, principal, minimum_role=ProjectMembership.Role.VIEWER, scope=scope)

def assert_viewer(project, principal, *, scope=None):
    return authorize_project(project, principal, minimum_role=ProjectMembership.Role.VIEWER, scope=scope)
