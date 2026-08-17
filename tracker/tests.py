from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import AgentToken, Profile, User
from accounts.services.tokens import digest_token
from projects.models import AuditEvent, Lead, LeadComment, LeadInterest, Project, ProjectMembership
from projects.services.authorization import SCOPES
from tracker.forms import LeadForm, RegisterForm


class TrackerWebFlowTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner@example.com", "owner-password")
        self.editor = User.objects.create_user("editor@example.com", "editor-password")
        self.viewer = User.objects.create_user("viewer@example.com", "viewer-password")
        self.outsider = User.objects.create_user("outsider@example.com", "outside-password")
        self.project = Project.objects.create(
            name="September search", slug="september-search", creator=self.owner
        )
        for user, role in (
            (self.owner, ProjectMembership.Role.OWNER),
            (self.editor, ProjectMembership.Role.EDITOR),
            (self.viewer, ProjectMembership.Role.VIEWER),
        ):
            ProjectMembership.objects.create(project=self.project, user=user, role=role)
            Profile.objects.create(user=user, display_name=user.email.partition("@")[0].title())
        self.lead = Lead.objects.create(
            project=self.project,
            creator=self.owner,
            source="example",
            canonical_url="https://example.test/listing/1",
            title="Sunny room",
            summary="A useful lead",
            location="Brooklyn",
            attributes={"guests": "okay", "parks": "nearby"},
        )

    def login_as(self, user):
        self.client.force_login(user)

    def test_every_collaborator_can_edit_project_content_and_invite(self):
        self.login_as(self.viewer)
        detail = self.client.get(reverse("tracker:lead-detail", args=[self.project.slug, self.lead.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "guests")
        self.assertContains(detail, "okay")
        self.assertEqual(self.client.get(reverse("tracker:prompt-edit", args=[self.project.slug])).status_code, 200)
        self.assertEqual(self.client.get(reverse("tracker:lead-edit", args=[self.project.slug, self.lead.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("tracker:project-settings", args=[self.project.slug])).status_code, 200)
        self.assertEqual(self.client.get(reverse("tracker:member-invite", args=[self.project.slug])).status_code, 200)

    def test_editor_can_edit_leads_and_invite_members(self):
        self.login_as(self.editor)
        self.assertEqual(
            self.client.get(reverse("tracker:lead-edit", args=[self.project.slug, self.lead.pk])).status_code,
            200,
        )
        self.assertEqual(self.client.get(reverse("tracker:member-invite", args=[self.project.slug])).status_code, 200)

    def test_owner_can_manage_project_and_create_prompt(self):
        self.login_as(self.owner)
        self.assertEqual(
            self.client.get(reverse("tracker:project-settings", args=[self.project.slug])).status_code,
            200,
        )
        prompt = self.client.post(
            reverse("tracker:prompt-edit", args=[self.project.slug]),
            {"prompt": "Find exact September dates", "criteria": "{}", "expected_revision": "0"},
        )
        self.assertEqual(prompt.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.prompt, "Find exact September dates")

    def test_only_owner_can_remove_a_member_after_confirmation(self):
        self.login_as(self.owner)
        settings_page = self.client.get(
            reverse("tracker:project-settings", args=[self.project.slug])
        )
        self.assertContains(settings_page, "Remove project member?")
        self.assertContains(settings_page, "data-member-remove")

        remove_url = reverse(
            "tracker:member-remove", args=[self.project.slug, self.viewer.pk]
        )
        self.login_as(self.editor)
        self.assertEqual(self.client.post(remove_url).status_code, 403)
        self.assertTrue(
            ProjectMembership.objects.filter(project=self.project, user=self.viewer).exists()
        )

        self.login_as(self.owner)
        response = self.client.post(remove_url)
        self.assertRedirects(
            response, reverse("tracker:project-settings", args=[self.project.slug])
        )
        self.assertFalse(
            ProjectMembership.objects.filter(project=self.project, user=self.viewer).exists()
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                project=self.project,
                action="membership.removed",
                object_id=str(self.viewer.pk),
            ).exists()
        )

    def test_final_owner_cannot_remove_themselves(self):
        ProjectMembership.objects.filter(project=self.project).exclude(user=self.owner).delete()
        self.login_as(self.owner)
        response = self.client.post(
            reverse("tracker:member-remove", args=[self.project.slug, self.owner.pk]),
            follow=True,
        )
        self.assertRedirects(
            response, reverse("tracker:project-settings", args=[self.project.slug])
        )
        self.assertTrue(
            ProjectMembership.objects.filter(project=self.project, user=self.owner).exists()
        )
        self.assertContains(response, "A project must retain at least one owner.")

    def test_outsider_cannot_discover_project(self):
        self.login_as(self.outsider)
        response = self.client.get(reverse("tracker:lead-list", args=[self.project.slug]))
        self.assertEqual(response.status_code, 404)

    def test_agent_setup_creates_user_wide_token_and_shows_secret_once(self):
        self.login_as(self.owner)
        response = self.client.post(
            reverse("tracker:agent-setup"),
            {"action": "create-key", "name": "Living-room agent"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        token = AgentToken.objects.get(user=self.owner, name="Living-room agent")
        self.assertEqual(token.project_ids, [])
        self.assertEqual(set(token.scopes), SCOPES - {"leads:destroy"})
        audit = AuditEvent.objects.get(action="agent_token.created", token_id=token.pk)
        self.assertEqual(audit.actor, self.owner)
        self.assertNotIn("token", audit.summary)

        secret = response.context["new_key"]
        self.assertTrue(secret)
        self.assertEqual(token.digest, digest_token(secret))
        self.assertNotIn(secret, response.context["agent_prompt"])

        later_project = Project.objects.create(
            name="Later search", slug="later-search", creator=self.owner
        )
        ProjectMembership.objects.create(
            project=later_project, user=self.owner, role=ProjectMembership.Role.OWNER
        )
        portfolio = self.client.get(
            "/api/v1/me/projects", HTTP_AUTHORIZATION=f"Bearer {secret}"
        )
        self.assertEqual(portfolio.status_code, 200)
        self.assertEqual(
            {item["id"] for item in portfolio.json()["items"]},
            {str(self.project.pk), str(later_project.pk)},
        )

        later_page = self.client.get(reverse("tracker:agent-setup"))
        self.assertIsNone(later_page.context["new_key"])
        self.assertNotContains(later_page, secret)

    @override_settings(PUBLIC_BASE_URL="https://homing.example")
    def test_agent_skill_is_valid_secret_free_download(self):
        self.login_as(self.owner)
        response = self.client.get(reverse("tracker:agent-skill-download"), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Content-Disposition", response)
        self.assertIn("public", response["Cache-Control"])
        self.assertNotIn("Cookie", response.get("Vary", ""))
        body = response.content.decode()
        self.assertTrue(body.startswith("---\nname: homing-setup\ndescription:"))
        self.assertIn("POST https://homing.example/api/v1/agent-link", body)
        self.assertIn("https://homing.example/api/v1", body)
        self.assertIn("GET /me/projects", body)
        self.assertIn("The person never pastes a key.", body)
        self.assertNotIn("owner@example.com", body)

    def test_agent_setup_requires_login_and_old_token_route_redirects(self):
        self.assertEqual(
            self.client.get(reverse("tracker:agent-setup")).status_code,
            302,
        )
        self.login_as(self.owner)
        response = self.client.get(reverse("tracker:token-create"))
        self.assertRedirects(response, reverse("tracker:agent-setup"))

    def test_interested_attribution_is_per_user_and_visible_to_members(self):
        self.login_as(self.viewer)
        response = self.client.post(
            reverse("tracker:lead-interest", args=[self.project.slug, self.lead.pk]),
            {"interested": "true", "next": reverse("tracker:lead-list", args=[self.project.slug])},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(LeadInterest.objects.filter(lead=self.lead, user=self.viewer).exists())

        page = self.client.get(reverse("tracker:lead-list", args=[self.project.slug]))
        self.assertContains(page, "Viewer")
        self.assertContains(page, 'aria-pressed="true"')
        self.assertNotContains(page, 'aria-pressed="false"')

        self.login_as(self.editor)
        page = self.client.get(reverse("tracker:lead-list", args=[self.project.slug]))
        self.assertContains(page, "Viewer")
        self.assertContains(page, 'aria-pressed="false"')

    def test_owner_can_delete_comment_and_editor_can_restore_trash(self):
        comment = LeadComment.objects.create(lead=self.lead, author=self.editor, body="Check guests")
        self.login_as(self.viewer)
        denied = self.client.post(
            reverse("tracker:comment-delete", args=[self.project.slug, self.lead.pk, comment.pk])
        )
        self.assertEqual(denied.status_code, 403)
        comment.refresh_from_db()
        self.assertIsNone(comment.deleted_at)

        self.login_as(self.owner)
        deleted_by_owner = self.client.post(
            reverse("tracker:comment-delete", args=[self.project.slug, self.lead.pk, comment.pk])
        )
        self.assertEqual(deleted_by_owner.status_code, 302)
        comment.refresh_from_db()
        self.assertIsNotNone(comment.deleted_at)

        comment = LeadComment.objects.create(lead=self.lead, author=self.editor, body="Check guests")
        self.login_as(self.editor)
        deleted = self.client.post(
            reverse(
                "tracker:comment-delete",
                args=[self.project.slug, self.lead.pk, comment.pk],
            )
        )
        self.assertEqual(deleted.status_code, 302)
        comment.refresh_from_db()
        self.assertIsNotNone(comment.deleted_at)

        self.client.post(
            reverse("tracker:lead-trash", args=[self.project.slug, self.lead.pk]),
            {"reason": "Dates no longer work"},
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.TRASHED)
        restored = self.client.post(
            reverse("tracker:lead-restore", args=[self.project.slug, self.lead.pk]),
            {"next": reverse("tracker:lead-list", args=[self.project.slug]) + "?status=trash"},
        )
        self.assertEqual(restored.status_code, 302)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.ACTIVE)

    def test_list_view_and_batch_trash_append_a_chronological_comment(self):
        second = Lead.objects.create(
            project=self.project,
            creator=self.owner,
            source="example",
            canonical_url="https://example.test/listing/2",
            title="Second lead",
        )
        self.login_as(self.viewer)
        list_page = self.client.get(
            reverse("tracker:lead-list", args=[self.project.slug]), {"view": "list"}
        )
        self.assertContains(list_page, "lead-list-rows")
        self.assertContains(list_page, "Second lead")

        response = self.client.post(
            reverse("tracker:lead-batch", args=[self.project.slug]),
            {
                "lead_ids": [str(self.lead.pk), str(second.pk)],
                "action": "trash",
                "comment": "Dates do not work for us.",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Lead.objects.filter(pk__in=[self.lead.pk, second.pk], status=Lead.Status.TRASHED).count(),
            2,
        )
        self.assertEqual(
            LeadComment.objects.filter(
                lead__in=[self.lead, second],
                author=self.viewer,
                body="Dates do not work for us.",
            ).count(),
            2,
        )

    def test_lead_edit_rejects_stale_revision_and_keeps_draft(self):
        self.login_as(self.editor)
        response = self.client.post(
            reverse("tracker:lead-edit", args=[self.project.slug, self.lead.pk]),
            {
                "source": "example",
                "source_listing_id": "",
                "canonical_url": self.lead.canonical_url,
                "source_url": "",
                "title": "Draft title",
                "summary": "A draft",
                "location": "Brooklyn",
                "price_display": "Unknown",
                "price_amount": "",
                "price_currency": "USD",
                "availability": "September",
                "housing_type": "unknown",
                "date_confidence": "unknown",
                "park_notes": "",
                "attributes": "{}",
                "verification_notes": "",
                "expected_revision": "0",
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "Draft title", status_code=409)


class RegistrationFormTests(TestCase):
    def test_public_signup_is_closed_by_default(self):
        login_response = self.client.get(reverse("login"))
        self.assertNotContains(login_response, "Create one")

        register_response = self.client.get(reverse("tracker:register"))
        self.assertEqual(register_response.status_code, 403)
        self.assertContains(register_response, "Public signup is currently closed", status_code=403)

    @override_settings(ALLOW_PUBLIC_SIGNUP=True)
    def test_signup_link_is_visible_when_enabled(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, "Create one")

    def test_registration_requires_twelve_character_password(self):
        form = RegisterForm(
            data={
                "email": "new@example.com",
                "password1": "eleven-char",
                "password2": "eleven-char",
            }
        )
        self.assertFalse(form.is_valid())

        short = RegisterForm(
            data={"email": "new@example.com", "password1": "short", "password2": "short"}
        )
        self.assertFalse(short.is_valid())
        self.assertIn("password1", short.errors)

    @override_settings(
        AUTH_PASSWORD_VALIDATORS=[
            {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"}
        ]
    )
    def test_registration_uses_configured_common_password_validator(self):
        form = RegisterForm(
            data={
                "email": "new@example.com",
                "password1": "password1234",
                "password2": "password1234",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("password1", form.errors)

    def test_lead_url_fields_use_https_for_bare_domains(self):
        form = LeadForm(
            data={
                "source": "example",
                "canonical_url": "example.test/listing/1",
                "source_url": "",
                "title": "Lead",
                "price_currency": "USD",
                "housing_type": "unknown",
                "date_confidence": "unknown",
                "attributes": "{}",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["canonical_url"], "https://example.test/listing/1")
