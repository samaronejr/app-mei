"""AXRM-015: a paid PAGTOWEB page is kept until its consultation settles it (A7.4).

A runner that paid for a page used to hold the parsed page only in memory, so any
takeover (its lease ran out and its session looked gone, or it died after paying)
could only finish the consultation as `incerto`. The page call's success now
inserts the page's normalized payment fields here in the same transaction, and
whichever runner settles the consultation adopts them.

Firm-side like its consultation (INV-R10): tenant RLS, the client-anchored policy
and DenyPortalAccess, client-paired composites to its consultation and its call.
The reverse drops the table on a populated database too: a kept page is transient
(applied, or deleted when its consultation ends), and the reversed code finishes a
consultation whose page it cannot read as `incerto`, as it did before.
"""

import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
from django.db import migrations, models

import apps.core.identifiers
from apps.core.migrations._composite_fk import add_client_composite_fk
from apps.core.migrations._operations import (
    DenyPortalAccess,
    EnablePortalClientRLS,
    EnableRLS,
)


class Migration(migrations.Migration):
    dependencies = [
        ("clients", "0015_import_batches_and_versions"),
        ("integrations", "0009_payment_query_owner_session"),
        ("tenants", "0015_provider_usage_metrics"),
    ]

    operations = [
        migrations.CreateModel(
            name="PaymentPage",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=apps.core.identifiers.uuid7,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("index", models.PositiveSmallIntegerField()),
                ("records", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "call",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="integrations.providercall",
                    ),
                ),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="clients.clientcompany",
                    ),
                ),
                (
                    "query",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="integrations.paymentquery",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="tenants.tenant",
                        verbose_name="tenant",
                    ),
                ),
            ],
            options={
                "abstract": False,
                "base_manager_name": "all_objects",
                "default_manager_name": "objects",
                "indexes": [
                    models.Index(
                        fields=["tenant", "client"], name="paymentpage_scope_idx"
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "client", "id"),
                        name="paymentpage_scope_id_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("tenant", "client", "query", "index"),
                        name="paymentpage_query_index_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("tenant", "client", "call"),
                        name="paymentpage_call_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("index__lt", 10)),
                        name="paymentpage_index_cap",
                    ),
                ],
            },
            managers=[
                ("all_objects", django.db.models.manager.Manager()),
                ("objects", django.db.models.manager.Manager()),
            ],
        ),
        EnableRLS("PaymentPage"),
        EnablePortalClientRLS("PaymentPage"),
        DenyPortalAccess("PaymentPage"),
        add_client_composite_fk(
            table="integrations_paymentpage",
            column="query_id",
            parent_table="integrations_paymentquery",
            constraint="paymentpage_query_fk",
        ),
        add_client_composite_fk(
            table="integrations_paymentpage",
            column="call_id",
            parent_table="integrations_providercall",
            constraint="paymentpage_call_fk",
        ),
        migrations.RunSQL(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON integrations_paymentpage "
            "TO app_runtime;",
            "REVOKE ALL ON integrations_paymentpage FROM app_runtime;",
        ),
    ]
