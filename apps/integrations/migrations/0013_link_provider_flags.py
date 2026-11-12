"""AXRM-019: the link keeps the provider's switches as last read back.

Additive and reversible: the column has a default, holds only allowlisted status
names with booleans and dates, and dropping it loses no evidence (the read-back
calls stay journaled).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add `ClientProviderLink.provider_flags`."""

    dependencies = [("integrations", "0012_sitfis_issue_reason")]

    operations = [
        migrations.AddField(
            model_name="clientproviderlink",
            name="provider_flags",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
