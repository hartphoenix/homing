from django.db import migrations, models


def backfill_display_names(apps, schema_editor):
    Profile = apps.get_model("accounts", "Profile")
    for profile in Profile.objects.select_related("user").filter(display_name="").iterator():
        local_part = profile.user.email.partition("@")[0].strip()
        profile.display_name = (local_part or "Member")[:120]
        profile.save(update_fields=["display_name"])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_auththrottle")]

    operations = [
        migrations.RunPython(backfill_display_names, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="profile",
            name="display_name",
            field=models.CharField(max_length=120),
        ),
    ]
