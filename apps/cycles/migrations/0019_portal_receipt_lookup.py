"""Allow only exact receipt matching, still confined by the existing client RLS."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("cycles", "0018_reminder_candidates")]

    operations = [
        migrations.RunSQL(
            "GRANT SELECT (tenant_id, publication_id, publication_digest, user_id) "
            "ON cycles_clientacknowledgement TO app_portal;",
            "REVOKE SELECT (tenant_id, publication_id, publication_digest, user_id) "
            "ON cycles_clientacknowledgement FROM app_portal;",
        ),
    ]
