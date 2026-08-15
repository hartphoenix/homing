from django.urls import path

from . import views

app_name = "tracker"

urlpatterns = [
    path("login/", views.TrackerLoginView.as_view(), name="login"),
    path("logout/", views.TrackerLogoutView.as_view(), name="logout"),
    path("register/", views.register, name="register"),
    path("projects/", views.project_list, name="project-list"),
    path("projects/new/", views.project_create, name="project-create"),
    path("projects/<slug:slug>/", views.project_detail, name="project-detail"),
    path("projects/<slug:slug>/edit/", views.project_edit, name="project-edit"),
    path("projects/<slug:slug>/settings/", views.project_settings, name="project-settings"),
    path("projects/<slug:slug>/prompt/", views.prompt_edit, name="prompt-edit"),
    path("projects/<slug:slug>/leads/", views.lead_list, name="lead-list"),
    path("projects/<slug:slug>/leads/new/", views.lead_create, name="lead-create"),
    path("projects/<slug:slug>/leads/<uuid:lead_id>/", views.lead_detail, name="lead-detail"),
    path("projects/<slug:slug>/leads/<uuid:lead_id>/edit/", views.lead_edit, name="lead-edit"),
    path("projects/<slug:slug>/leads/<uuid:lead_id>/trash/", views.lead_trash, name="lead-trash"),
    path(
        "projects/<slug:slug>/leads/<uuid:lead_id>/restore/",
        views.lead_restore,
        name="lead-restore",
    ),
    path(
        "projects/<slug:slug>/leads/<uuid:lead_id>/interest/",
        views.lead_interest,
        name="lead-interest",
    ),
    path(
        "projects/<slug:slug>/leads/<uuid:lead_id>/comments/",
        views.comment_create,
        name="comment-create",
    ),
    path(
        "projects/<slug:slug>/leads/<uuid:lead_id>/comments/<int:comment_id>/edit/",
        views.comment_edit,
        name="comment-edit",
    ),
    path(
        "projects/<slug:slug>/leads/<uuid:lead_id>/comments/<int:comment_id>/delete/",
        views.comment_delete,
        name="comment-delete",
    ),
    path("projects/<slug:slug>/invite/", views.member_invite, name="member-invite"),
    path("invite/<str:token>/", views.invitation_accept, name="invitation-accept"),
    path("profile/", views.profile, name="profile"),
    path("agent-setup/", views.agent_setup, name="agent-setup"),
    path("agent-setup/SKILL.md", views.agent_skill_download, name="agent-skill-download"),
    path("profile/tokens/new/", views.token_create, name="token-create"),
    path("profile/tokens/<uuid:token_id>/revoke/", views.token_revoke, name="token-revoke"),
    path("saved-prompts/", views.saved_prompts, name="saved-prompts"),
    path("saved-prompts/new/", views.saved_prompt_create, name="saved-prompt-create"),
    path("saved-prompts/<int:prompt_id>/edit/", views.saved_prompt_edit, name="saved-prompt-edit"),
]
