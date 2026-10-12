"""Tenant-led source index for bounded overdue reminder candidate reads."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("cycles", "0017_bulk_operations")]

    operations = [
        migrations.AddIndex(
            model_name="evidencerequest",
            index=models.Index(
                fields=["tenant", "state", "due_on", "id"],
                name="evidencerequest_reminder_idx",
            ),
        ),
    ]
