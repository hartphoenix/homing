from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import User


class LoginCsrfRecoveryTests(TestCase):
    def test_login_page_is_not_cached_with_a_stale_csrf_form(self):
        response = self.client.get(reverse("login"))

        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["Pragma"], "no-cache")

    def test_rejected_login_csrf_has_a_safe_fresh_page_path(self):
        user = User.objects.create_user("owner@example.com", "owner-password")
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("login"))

        response = client.post(
            reverse("login"),
            {
                "username": user.email,
                "password": "owner-password",
                "csrfmiddlewaretoken": "stale-token",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Sign-in page expired", status_code=403)
        self.assertContains(response, 'href="/login/"', status_code=403)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["Pragma"], "no-cache")
        self.assertFalse(client.session.get("_auth_user_id"))
