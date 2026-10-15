"""Preserve each archive's exclusion notice independently of later scan results."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("cycles", "0019_portal_receipt_lookup"),
    ]

    operations = [
        migrations.AddField(
            model_name="evidenceexport",
            name="excluded_originals",
            field=models.JSONField(default=list),
        ),
    ]
