"""The hourly capture sweep's client cursor shares the existing tenant control."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("delivery", "0009_federal_sweep_cursors")]

    operations = [
        migrations.AddField(
            model_name="automationpause",
            name="capture_documents_cursor",
            field=models.UUIDField(blank=True, null=True),
        )
    ]
