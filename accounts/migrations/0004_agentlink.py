import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_profile_display_name_required"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="agent_paused_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="agenttoken",
            name="expected_cadence_minutes",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="agenttoken",
            name="environment_note",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="agenttoken",
            name="exposed_to_chat",
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name="AgentLink",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("device_code_hash", models.CharField(editable=False, max_length=64, unique=True)),
                ("user_code", models.CharField(db_index=True, max_length=8)),
                ("agent_label", models.CharField(max_length=120)),
                ("environment_note", models.CharField(blank=True, max_length=200)),
                ("requested_cadence_minutes", models.PositiveIntegerField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("approved", "Approved"),
                            ("denied", "Denied"),
                            ("expired", "Expired"),
                            ("consumed", "Consumed"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("interval_seconds", models.PositiveIntegerField(default=5)),
                ("poll_count", models.PositiveIntegerField(default=0)),
                ("last_polled_at", models.DateTimeField(blank=True, null=True)),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="approved_agent_links",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "issued_token",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="agent_links",
                        to="accounts.agenttoken",
                    ),
                ),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.AddIndex(
            model_name="agentlink",
            index=models.Index(
                fields=["status", "expires_at"], name="accounts_agentlink_status_idx"
            ),
        ),
    ]
