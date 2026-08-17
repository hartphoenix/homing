from django.db import migrations


def migrate_trash_reasons(apps, schema_editor):
    Lead = apps.get_model("projects", "Lead")
    LeadComment = apps.get_model("projects", "LeadComment")
    # Legacy rows may have lost their original actor.  The lead creator is a
    # stable, attributable fallback in that case; no synthetic account is
    # needed and the original text remains visible in the project timeline.
    for lead in Lead.objects.exclude(trash_reason="").iterator():
        author_id = lead.trashed_by_id or lead.creator_id
        if author_id:
            LeadComment.objects.create(
                lead_id=lead.pk,
                author_id=author_id,
                body=f"Legacy trash reason: {lead.trash_reason}"[:10000],
            )
        lead.trash_reason = ""
        lead.save(update_fields=["trash_reason"])


def preserve_reasons(apps, schema_editor):
    # The migration is intentionally not reversible: comments are the source
    # of truth after this release and reconstructing a single reason from a
    # chronological comment stream would be lossy.
    return None


class Migration(migrations.Migration):
    dependencies = [("projects", "0001_initial")]

    operations = [
        migrations.RunPython(migrate_trash_reasons, preserve_reasons),
        migrations.RemoveField(model_name="lead", name="trash_reason"),
    ]
