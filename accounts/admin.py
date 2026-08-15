from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import AgentToken, Profile, SavedPrompt, User


@admin.register(User)
class UserAdmin(UserAdmin):
    ordering = ("email",)
    list_display = ("email", "is_active", "is_staff", "date_joined")
    search_fields = ("email",)
    fieldsets = ((None, {"fields": ("email", "password")}), ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}), ("Dates", {"fields": ("last_login", "date_joined", "updated_at")}))
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("email", "password1", "password2", "is_staff", "is_active")} ),)
    readonly_fields = ("date_joined", "updated_at", "last_login")


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("display_name", "user", "timezone", "updated_at")
    search_fields = ("display_name", "user__email")


@admin.register(SavedPrompt)
class SavedPromptAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "updated_at")
    search_fields = ("title", "user__email")


@admin.register(AgentToken)
class AgentTokenAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "token_prefix", "expires_at", "revoked_at", "last_used_at")
    search_fields = ("name", "token_prefix", "user__email")
    readonly_fields = ("digest", "token_prefix", "created_at", "last_used_at")
