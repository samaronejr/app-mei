"""Confine `app_portal` to a single client on the client-scoped tables of this app.

These tables already carry a permissive tenant policy from `EnableRLS`, and a composite
`(tenant_id, client_id)` foreign key that makes `client_id` and `tenant_id` name the same
firm. The RESTRICTIVE policies added here AND with that tenant policy and bind only
`app_portal`, so firm-side reads are unchanged.
"""

from django.db import migrations

from apps.core.migrations._operations import EnablePortalClientRLS


class Migration(migrations.Migration):
    dependencies = [
        ("clients", "0007_clientcompany_mei_category"),
    ]

    operations = [
        EnablePortalClientRLS("ClientAssignment"),
        EnablePortalClientRLS("ClientTag"),
        EnablePortalClientRLS("OnboardingItem"),
    ]
