"""Add outcome metadata without inventing a successful run or historical consent."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("obligations", "0021_document_purged_at_document_retention_class"),
    ]

    operations = [
        migrations.AddField(
            model_name="schedulerheartbeat",
            name="last_attempt_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="schedulerheartbeat",
            name="last_success_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="schedulerheartbeat",
            name="last_error_code",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="schedulerheartbeat",
            name="outcomes_json",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
