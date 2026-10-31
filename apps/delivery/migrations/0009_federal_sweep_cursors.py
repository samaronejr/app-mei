"""Per-firm cursors for the monthly federal sweeps (AXRM-018)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("delivery", "0008_automatic_reminder_authority")]

    operations = [
        migrations.AddField(
            model_name="automationpause",
            name="federal_sitfis_cursor",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="automationpause",
            name="federal_payments_cursor",
            field=models.UUIDField(blank=True, null=True),
        ),
    ]
