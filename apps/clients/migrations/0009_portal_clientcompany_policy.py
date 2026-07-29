"""Let `app_portal` see exactly one row of `clients_clientcompany`: its own.

This table carries no `client_id` because it *is* the client. Its primary key is the
client identity, so the predicate compares `id` against `app.client_id` rather than the
`client_id` column every other client-scoped table uses.
"""

from django.db import migrations

from apps.core.migrations._operations import EnablePortalClientRLS


class Migration(migrations.Migration):
    dependencies = [
        ("clients", "0008_portal_client_policies"),
    ]

    operations = [
        EnablePortalClientRLS("ClientCompany", tenant_field="id"),
    ]
