"""Keep an invitation's exact contact origin across delivery token refreshes.

No email-derived backfill: old address-only invitations have no proven origin.
Contacts are tenant/client paired. A contact origin requires a non-null client,
so MATCH SIMPLE cannot bypass that pairing by omitting the client dimension.
RESTRICT preserves invitation history rather than cascading a contact deletion.
No new table, RLS policy, portal grant, or frozen message content is introduced.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("clients", "0015_import_batches_and_versions"),
        ("tenants", "0013_backfill_billing_accounts"),
    ]

    operations = [
        migrations.AddField(
            model_name="invite",
            name="contact",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="portal_invitations",
                to="clients.clientcontact",
                verbose_name="originating contact",
            ),
        ),
        migrations.AddConstraint(
            model_name="invite",
            constraint=models.CheckConstraint(
                condition=models.Q(contact__isnull=True)
                | models.Q(client__isnull=False),
                name="invite_contact_requires_client",
            ),
        ),
        migrations.RunSQL(
            sql=(
                'ALTER TABLE "tenants_invite" '
                'ADD CONSTRAINT "invite_tenant_client_contact_fk" '
                'FOREIGN KEY ("tenant_id", "client_id", "contact_id") '
                'REFERENCES "clients_clientcontact" ("tenant_id", "client_id", "id") '
                "ON DELETE RESTRICT"
            ),
            reverse_sql=(
                'ALTER TABLE "tenants_invite" '
                'DROP CONSTRAINT "invite_tenant_client_contact_fk"'
            ),
        ),
    ]
