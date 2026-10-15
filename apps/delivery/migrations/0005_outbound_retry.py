"""Explicit successors preserve the original immutable rejection barrier."""

import django.db.models.deletion
from django.db import migrations, models

from apps.core.migrations._composite_fk import add_client_composite_fk


class Migration(migrations.Migration):
    dependencies = [("delivery", "0004_outbox_health")]

    operations = [
        migrations.AddField(
            model_name="outboundmessage",
            name="retry_of",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="retries",
                to="delivery.outboundmessage",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.UniqueConstraint(
                fields=("tenant", "client", "retry_of"),
                name="outbound_retry_parent_uniq",
            ),
        ),
        add_client_composite_fk(
            table="delivery_outboundmessage",
            column="retry_of_id",
            parent_table="delivery_outboundmessage",
            constraint="outbound_retry_client_fk",
        ),
        # Reapplying after a populated rollback recovers the dropped link from
        # the immutable decision written in the same transaction as the retry.
        # Reversal drops the column, never the audit or delivery receipts.
        migrations.RunSQL(
            """
            UPDATE public.delivery_outboundmessage AS child
            SET retry_of_id = parent.id
            FROM public.audit_event AS decision
            JOIN public.delivery_outboundmessage AS parent
                ON decision.object_id = parent.id::text
                AND decision.tenant_id = parent.tenant_id
            WHERE decision.action = 'delivery.dead_letter_retried'
                AND decision.object_type = 'OutboundMessage'
                AND decision.metadata->>'retry_id' = child.id::text
                AND child.tenant_id = parent.tenant_id
                AND child.client_id = parent.client_id
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
