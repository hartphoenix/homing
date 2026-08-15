from django.urls import path

from . import views

app_name = "api"

urlpatterns = [
    path("auth/register", views.register),
    path("auth/token", views.token_exchange),
    path("auth/tokens", views.tokens),
    path("auth/tokens/<uuid:token_id>", views.token_detail),
    path("me", views.me),
    path("me/profile", views.profile),
    path("me/saved-prompts", views.saved_prompts),
    path("me/projects", views.my_projects),
    path("projects", views.projects),
    path("projects/<uuid:project_id>", views.project_detail),
    path("projects/<uuid:project_id>/members", views.members),
    path("projects/<uuid:project_id>/invitations", views.invitations),
    path("invitations/<str:invitation_token>/accept", views.accept_invitation),
    path("projects/<uuid:project_id>/prompt", views.prompt),
    path("projects/<uuid:project_id>/prompt-revisions", views.prompt_revisions),
    path("projects/<uuid:project_id>/changes", views.changes),
    path("projects/<uuid:project_id>/search-runs", views.search_runs),
    path("projects/<uuid:project_id>/search-runs/<uuid:run_id>", views.search_run_detail),
    path("projects/<uuid:project_id>/search-runs/<uuid:run_id>/claim", views.claim_run),
    path("projects/<uuid:project_id>/search-runs/<uuid:run_id>/heartbeat", views.heartbeat_run),
    path("projects/<uuid:project_id>/search-runs/<uuid:run_id>/complete", views.complete_run),
    path("projects/<uuid:project_id>/leads", views.leads),
    path("projects/<uuid:project_id>/leads/bulk-upsert", views.bulk_upsert),
    path("projects/<uuid:project_id>/leads/<uuid:lead_id>", views.lead_detail),
    path("projects/<uuid:project_id>/leads/<uuid:lead_id>/interest", views.interest),
    path("projects/<uuid:project_id>/leads/<uuid:lead_id>/comments", views.comments),
    path(
        "projects/<uuid:project_id>/leads/<uuid:lead_id>/comments/<int:comment_id>",
        views.comment_detail,
    ),
    path("projects/<uuid:project_id>/interested", views.interested),
    path("projects/<uuid:project_id>/trash", views.trash),
    path("projects/<uuid:project_id>/trash/<uuid:lead_id>/restore", views.restore),
]
