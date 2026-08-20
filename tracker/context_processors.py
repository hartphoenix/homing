from .source_plan_reviews import open_review_count


def source_plan_review_banner(request):
    """Expose only the bounded count needed by the authenticated site banner."""
    return {
        "source_plan_review_count": open_review_count(getattr(request, "user", None)),
    }
