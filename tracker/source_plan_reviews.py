"""Read-only projections of source-plan review state for browser UI.

The review model belongs to the projects domain. Keep this module defensive while a
deployment is between code and migration rollout: a missing review table must not make
every authenticated page fail.
"""

from django.apps import apps
from django.db.utils import OperationalError, ProgrammingError


def _review_model():
    try:
        return apps.get_model("projects", "SourcePlanReview")
    except LookupError:
        return None


def _open_reviews(user):
    if not user or not getattr(user, "is_authenticated", False):
        return None
    model = _review_model()
    if model is None:
        return None
    # Reviews are user-owned. Membership and active-project filters keep an old
    # review from leaking through a removed collaboration or trashed project.
    return model.objects.filter(
        user=user,
        status="open",
        project__status="active",
        project__memberships__user=user,
    ).distinct()


def open_review_count(user):
    queryset = _open_reviews(user)
    if queryset is None:
        return 0
    try:
        return queryset.count()
    except (OperationalError, ProgrammingError):
        # During a rolling deploy the code can briefly precede the migration.
        return 0
