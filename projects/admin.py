from django.contrib import admin

from .models import (AuditEvent, IdempotencyKey, Lead, LeadComment, LeadInterest,
                     Project, ProjectChange, ProjectInvitation, ProjectMembership,
                     PromptRevision, SearchRun, SourcePlanReview)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "status", "creator", "prompt_revision", "updated_at")
    list_filter = ("status",)
    search_fields = ("name", "slug", "creator__email")


@admin.register(ProjectMembership)
class ProjectMembershipAdmin(admin.ModelAdmin):
    list_display = ("project", "user", "role", "joined_at")
    list_filter = ("role",)
    search_fields = ("project__name", "user__email")


@admin.register(ProjectInvitation)
class ProjectInvitationAdmin(admin.ModelAdmin):
    list_display = ("project", "invited_email", "role", "expires_at", "accepted_at", "revoked_at")
    list_filter = ("role",)
    search_fields = ("project__name", "invited_email")
    readonly_fields = ("token_digest", "created_at")


@admin.register(PromptRevision)
class PromptRevisionAdmin(admin.ModelAdmin):
    list_display = ("project", "revision", "editor", "created_at")
    search_fields = ("project__name", "editor__email")
    readonly_fields = ("created_at",)


@admin.register(SearchRun)
class SearchRunAdmin(admin.ModelAdmin):
    list_display = ("project", "status", "user", "agent_label", "created_at", "completed_at")
    list_filter = ("status",)
    search_fields = ("project__name", "user__email", "agent_label")


@admin.register(SourcePlanReview)
class SourcePlanReviewAdmin(admin.ModelAdmin):
    list_display = (
        "project", "user", "status", "observed_prompt_revision", "last_reported_at"
    )
    list_filter = ("status",)
    search_fields = ("project__name", "user__email")
    readonly_fields = ("opened_at", "last_reported_at", "resolved_at")


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("title", "project", "source", "status", "date_confidence", "revision", "updated_at")
    list_filter = ("status", "housing_type", "date_confidence")
    search_fields = ("title", "location", "source", "project__name")


@admin.register(LeadInterest)
class LeadInterestAdmin(admin.ModelAdmin):
    list_display = ("lead", "user", "created_at")
    search_fields = ("lead__title", "user__email")


@admin.register(LeadComment)
class LeadCommentAdmin(admin.ModelAdmin):
    list_display = ("lead", "author", "created_at", "edited_at", "deleted_at")
    search_fields = ("lead__title", "author__email", "body")


@admin.register(ProjectChange)
class ProjectChangeAdmin(admin.ModelAdmin):
    list_display = ("project", "sequence", "event_type", "actor_kind", "created_at")
    list_filter = ("event_type", "actor_kind", "tombstone")
    search_fields = ("project__name", "object_id")
    readonly_fields = ("created_at",)


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("project", "action", "actor_kind", "request_id", "created_at")
    list_filter = ("action", "actor_kind")
    search_fields = ("project__name", "request_id", "object_id")
    readonly_fields = ("created_at",)


@admin.register(IdempotencyKey)
class IdempotencyKeyAdmin(admin.ModelAdmin):
    list_display = ("user", "endpoint", "key", "response_status", "expires_at")
    search_fields = ("user__email", "endpoint", "key")
    readonly_fields = ("created_at",)
