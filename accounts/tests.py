from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from accounts.models import Profile, User
from accounts.services.tokens import create_agent_token, digest_token, revoke_agent_token


class AccountModelTests(TestCase):
    def test_email_is_normalized_and_profile_is_private(self):
        user = User.objects.create_user("  Person@Example.COM ", "secret")
        self.assertEqual(user.email, "person@example.com")
        profile = Profile.objects.create(user=user, personal_details={"phone": "private"})
        self.assertEqual(profile.user_id, user.pk)

    def test_raw_token_is_only_returned_at_creation_and_can_be_revoked(self):
        user = User.objects.create_user("agent@example.com", "secret")
        token, raw = create_agent_token(user=user, name="cron", scopes=["leads:read"], expires_at=timezone.now() + timedelta(days=1))
        self.assertEqual(token.digest, digest_token(raw))
        self.assertTrue(token.is_valid)
        revoke_agent_token(token)
        token.refresh_from_db()
        self.assertFalse(token.is_valid)
