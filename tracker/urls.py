from django.urls import path
from django.views.generic import RedirectView

from . import views
from .auth_views import (
    TrackerPasswordResetCompleteView,
    TrackerPasswordResetConfirmView,
    TrackerPasswordResetDoneView,
    ThrottledPasswordResetView,
)

app_name = "tracker"

urlpatterns = [
    path("login/", views.TrackerLoginView.as_view(), name="login"),
    path("logout/", views.TrackerLogoutView.as_view(), name="logout"),
    path("register/", views.register, name="register"),
    path("password-reset/", ThrottledPasswordResetView.as_view(), name="password-reset"),
    path("password-reset/done/", TrackerPasswordResetDoneView.as_view(), name="password-reset-done"),
    path(
        "password-reset/<uidb64>/<token>/",
        TrackerPasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
    path(
        "password-reset/complete/",
        TrackerPasswordResetCompleteView.as_view(),
        name="password-reset-complete",
    ),
    path("projects/", views.project_list, name="project-list"),
    path("projects/new/", views.project_create, name="project-create"),
    path("projects/<slug:slug>/", views.project_detail, name="project-detail"),
    path("projects/<slug:slug>/edit/", views.project_edit, name="project-edit"),
    path("projects/<slug:slug>/settings/", views.project_settings, name="project-settings"),
    path("projects/<slug:slug>/delete/", views.project_delete, name="project-delete"),
    path("projects/<slug:slug>/prompt/", views.prompt_edit, name="prompt-edit"),
    path("projects/<slug:slug>/leads/", views.lead_list, name="lead-list"),
    path("projects/<slug:slug>/leads/batch/", views.lead_batch, name="lead-batch"),
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
    path(
        "projects/<slug:slug>/members/<int:user_id>/remove/",
        views.member_remove,
        name="member-remove",
    ),
    path("invite/<str:token>/", views.invitation_accept, name="invitation-accept"),
    path("profile/", views.profile, name="profile"),
    path("agent-setup/", views.agent_setup, name="agent-setup"),
    # The skill is public information served by the agentkit app.  Anything a
    # person already pasted into an assistant keeps working via this redirect,
    # and unlike the old login-walled download an agent can actually follow it.
    path(
        "agent-setup/SKILL.md",
        RedirectView.as_view(url="/agent/pkg/SKILL.md", permanent=True),
        name="agent-skill-download",
    ),
    path("link/", views.agent_link, name="agent-link"),
    path("profile/tokens/new/", views.token_create, name="token-create"),
    path("profile/tokens/<uuid:token_id>/revoke/", views.token_revoke, name="token-revoke"),
    path("saved-prompts/", views.saved_prompts, name="saved-prompts"),
    path("saved-prompts/new/", views.saved_prompt_create, name="saved-prompt-create"),
    path("saved-prompts/<int:prompt_id>/edit/", views.saved_prompt_edit, name="saved-prompt-edit"),
]
