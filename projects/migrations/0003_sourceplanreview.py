import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("accounts", "0004_agentlink"),
        ("projects", "0002_trash_reasons_to_comments"),
    ]

    operations = [
        migrations.CreateModel(
            name="SourcePlanReview",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("status", models.CharField(choices=[("open", "Open"), ("resolved", "Resolved")], default="open", max_length=16)),
                ("observed_prompt_revision", models.PositiveIntegerField(default=0)),
                ("resolved_prompt_revision", models.PositiveIntegerField(blank=True, null=True)),
                ("opened_at", models.DateTimeField(auto_now_add=True)),
                ("last_reported_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="source_plan_reviews", to="projects.project")),
                ("reporting_agent_token", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="reported_source_plan_reviews", to="accounts.agenttoken")),
                ("resolving_agent_token", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="resolved_source_plan_reviews", to="accounts.agenttoken")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="source_plan_reviews", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ("-last_reported_at", "-opened_at", "-id"),
            },
        ),
        migrations.AddConstraint(
            model_name="sourceplanreview",
            constraint=models.UniqueConstraint(
                condition=models.Q(status="open"),
                fields=("user", "project"),
                name="projects_sourceplanreview_open_user_project_uniq",
            ),
        ),
        migrations.AddIndex(
            model_name="sourceplanreview",
            index=models.Index(fields=("user", "status", "last_reported_at"), name="projects_so_user_id_76ebb9_idx"),
        ),
        migrations.AddIndex(
            model_name="sourceplanreview",
            index=models.Index(fields=("project", "status"), name="projects_so_project_462ec9_idx"),
        ),
    ]
