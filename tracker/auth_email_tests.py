import re
from urllib.parse import urlencode

from django.conf import settings
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Profile, User
from projects.models import Project, ProjectInvitation, ProjectMembership


@override_settings(
    ALLOW_PUBLIC_SIGNUP=False,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="Homing <notifications@example.test>",
)
class BrowserAuthEmailTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner@example.com", "owner-password")
        Profile.objects.create(user=self.owner, display_name="Owner")
        self.project = Project.objects.create(
            name="September search", slug="september-search", creator=self.owner
        )
        ProjectMembership.objects.create(
            project=self.project, user=self.owner, role=ProjectMembership.Role.OWNER
        )

    def test_invite_can_register_when_public_signup_is_closed_and_email_is_sent(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("tracker:member-invite", args=[self.project.slug]),
            {"email": "new@example.com", "role": "viewer"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        invitation = ProjectInvitation.objects.get(invited_email="new@example.com")
        token = re.search(r"/invite/([^/]+)/", mail.outbox[0].body).group(1)

        self.client.logout()
        invite_link = reverse("tracker:invitation-accept", args=[token])
        landing = self.client.get(invite_link)
        self.assertRedirects(
            landing,
            reverse("tracker:register") + f"?invite={token}",
            fetch_redirect_response=False,
        )
        register = self.client.get(landing["Location"])
        self.assertEqual(register.status_code, 200)
        self.assertContains(register, 'value="new@example.com"')
        self.assertContains(register, "readonly")
        tampered = self.client.post(
            reverse("tracker:register"),
            {
                "invite": token,
                "email": "someone-else@example.com",
                "display_name": "Wrong recipient",
                "password1": "a sufficiently long password",
                "password2": "a sufficiently long password",
            },
        )
        self.assertContains(tampered, "Use the invited email address")
        self.assertFalse(User.objects.filter(email="someone-else@example.com").exists())
        created = self.client.post(
            reverse("tracker:register"),
            {
                "invite": token,
                "email": "new@example.com",
                "display_name": "New teammate",
                "password1": "a sufficiently long password",
                "password2": "a sufficiently long password",
            },
        )
        self.assertRedirects(
            created,
            reverse("tracker:invitation-accept", args=[token]),
            fetch_redirect_response=False,
        )
        self.assertTrue(self.client.session.get("_auth_user_id"))
        self.client.post(reverse("tracker:invitation-accept", args=[token]))
        self.assertTrue(
            ProjectMembership.objects.filter(
                project=self.project, user__email="new@example.com"
            ).exists()
        )
        self.assertEqual(Profile.objects.get(user__email="new@example.com").display_name, "New teammate")
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.accepted_at)

    def test_existing_account_invitation_uses_same_landing_with_returning_sign_in(self):
        recipient = User.objects.create_user("recipient@example.com", "recipient-password")
        Profile.objects.create(user=recipient, display_name="Recipient")
        self.client.force_login(self.owner)
        self.client.post(
            reverse("tracker:member-invite", args=[self.project.slug]),
            {"email": recipient.email},
        )
        token = re.search(r"/invite/([^/]+)/", mail.outbox[-1].body).group(1)
        invite_link = reverse("tracker:invitation-accept", args=[token])

        self.client.logout()
        landing = self.client.get(invite_link)
        self.assertRedirects(
            landing,
            reverse("tracker:register") + f"?invite={token}",
            fetch_redirect_response=False,
        )
        registration = self.client.get(landing["Location"])
        expected_query = urlencode({"next": invite_link})
        self.assertContains(
            registration,
            f'{reverse("login")}?{expected_query}',
        )

    def test_registration_requires_a_nonempty_nickname(self):
        response = self.client.post(
            reverse("tracker:register"),
            {
                "email": "new@example.com",
                "display_name": "  ",
                "password1": "a sufficiently long password",
                "password2": "a sufficiently long password",
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_non_owner_collaborator_can_invite_and_invitation_remains_acceptable(self):
        collaborator = User.objects.create_user("collaborator@example.com", "collaborator-password")
        Profile.objects.create(user=collaborator, display_name="Collaborator")
        ProjectMembership.objects.create(
            project=self.project,
            user=collaborator,
            role=ProjectMembership.Role.VIEWER,
        )
        recipient = User.objects.create_user("recipient@example.com", "recipient-password")
        Profile.objects.create(user=recipient, display_name="Recipient")

        self.client.force_login(collaborator)
        response = self.client.post(
            reverse("tracker:member-invite", args=[self.project.slug]),
            {"email": recipient.email},
        )
        self.assertEqual(response.status_code, 200)
        token = re.search(r"/invite/([^/]+)/", mail.outbox[-1].body).group(1)
        self.client.force_login(recipient)
        accepted = self.client.post(reverse("tracker:invitation-accept", args=[token]))
        self.assertRedirects(
            accepted,
            reverse("tracker:project-detail", args=[self.project.slug]),
            fetch_redirect_response=False,
        )
        self.assertTrue(
            ProjectMembership.objects.filter(project=self.project, user=recipient).exists()
        )

    def test_password_reset_is_generic_and_reset_link_is_one_use(self):
        self.assertEqual(settings.PASSWORD_RESET_TIMEOUT, 15 * 60)
        response = self.client.post(
            reverse("tracker:password-reset"), {"email": self.owner.email}
        )
        self.assertRedirects(response, reverse("tracker:password-reset-done"))
        self.assertEqual(len(mail.outbox), 1)
        known = mail.outbox[0].body
        token_url = re.search(r"(http[^\s]+/password-reset/[^\s]+/)", known).group(1)
        unknown = self.client.post(
            reverse("tracker:password-reset"), {"email": "unknown@example.com"}
        )
        self.assertRedirects(unknown, reverse("tracker:password-reset-done"))
        self.assertEqual(len(mail.outbox), 1)

        confirm = self.client.get(token_url, follow=True)
        self.assertContains(confirm, "Choose a new password")
        token_url = confirm.request["PATH_INFO"]
        changed = self.client.post(
            token_url,
            {
                "new_password1": "a newer sufficiently long password",
                "new_password2": "a newer sufficiently long password",
            },
        )
        self.assertRedirects(changed, reverse("tracker:password-reset-complete"))
        reused = self.client.get(token_url, follow=True)
        self.assertContains(reused, "no longer valid")
