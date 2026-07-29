"""Confine `app_portal` to a single client on the client-scoped tables of this app.

Both tables already carry a permissive tenant policy from `EnableRLS` and a composite
`(tenant_id, client_id)` foreign key to `clients_clientcompany`, so the predicate below
cannot be satisfied by a row belonging to another firm.
"""

from django.db import migrations

from apps.core.migrations._operations import EnablePortalClientRLS


class Migration(migrations.Migration):
    dependencies = [
        ("obligations", "0008_monthly_revenue"),
    ]

    operations = [
        EnablePortalClientRLS("Obligation"),
        EnablePortalClientRLS("MonthlyRevenue"),
    ]
