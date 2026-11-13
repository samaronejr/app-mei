"""AXRM-021: immutable, private reviews of an exact client fiscal revision."""

import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
from django.apps import registry as app_registry
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.exceptions import IrreversibleError

from apps.audit.appendonly import append_only_reverse_sql, append_only_sql
from apps.core.identifiers import uuid7
from apps.core.migrations._composite_fk import (
    add_client_composite_fk,
    add_tenant_composite_fk,
)
from apps.core.migrations._operations import (
    DenyPortalAccess,
    EnablePortalClientRLS,
    EnableRLS,
)

TABLE = "cycles_fiscaldocumentreview"


def refuse_reverse_with_rows(
    apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None
) -> None:
    """A professional review survives flag-off recovery."""
    # CROSS_TENANT_OK: a review in any firm forbids deleting the review table.
    if apps.get_model("cycles", "FiscalDocumentReview")._base_manager.exists():
        message = "Fiscal reviews exist; keep them and use flag off and forward repair."
        raise IrreversibleError(message)


class Migration(migrations.Migration):
    dependencies = [
        ("cycles", "0022_federal_report_review"),
        ("obligations", "0029_fiscal_register"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FiscalDocumentReview",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid7, editable=False, primary_key=True, serialize=False
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
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="clients.clientcompany",
                        related_name="+",
                    ),
                ),
                (
                    "document",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="obligations.fiscaldocument",
                        related_name="+",
                    ),
                ),
                (
                    "revision",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="obligations.fiscalrevision",
                        related_name="+",
                    ),
                ),
                (
                    "cycle",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="cycles.servicecycle",
                        related_name="+",
                    ),
                ),
                (
                    "decision",
                    models.CharField(
                        max_length=24,
                        choices=[
                            (value, value)
                            for value in (
                                "aceito",
                                "rejeitado",
                                "requer_xml",
                                "documento_de_terceiro",
                            )
                        ],
                    ),
                ),
                (
                    "reviewer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                        related_name="+",
                    ),
                ),
                ("input_digest", models.CharField(max_length=64)),
                ("note", models.TextField(blank=True, max_length=2000)),
                (
                    "reviewed_at",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                ("version", models.PositiveIntegerField(default=1)),
            ],
            options={
                "abstract": False,
                "base_manager_name": "all_objects",
                "default_manager_name": "objects",
                "indexes": [
                    models.Index(
                        fields=["tenant", "client"],
                        name="fiscaldocumentreview_scope_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "client", "id"),
                        name="fiscaldocumentreview_scope_id_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("tenant", "client", "revision", "cycle"),
                        name="fiscaldocumentreview_revision_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            decision__in=[
                                "aceito",
                                "rejeitado",
                                "requer_xml",
                                "documento_de_terceiro",
                            ]
                        ),
                        name="fiscaldocumentreview_known_decision",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(input_digest__regex="^[a-f0-9]{64}$"),
                        name="fiscaldocumentreview_input_digest",
                    ),
                ],
            },
            managers=[
                ("all_objects", django.db.models.manager.Manager()),
                ("objects", django.db.models.manager.Manager()),
            ],
        ),
        EnableRLS("FiscalDocumentReview"),
        EnablePortalClientRLS("FiscalDocumentReview"),
        DenyPortalAccess("FiscalDocumentReview"),
        add_tenant_composite_fk(
            table=TABLE,
            column="client_id",
            parent_table="clients_clientcompany",
            constraint="fiscaldocumentreview_client_pair_fk",
        ),
        *[
            add_client_composite_fk(
                table=TABLE,
                column=column,
                parent_table=parent,
                constraint=f"fiscaldocumentreview_{column}_scope_fk",
            )
            for column, parent in (
                ("document_id", "obligations_fiscaldocument"),
                ("revision_id", "obligations_fiscalrevision"),
                ("cycle_id", "cycles_servicecycle"),
            )
        ],
        migrations.RunSQL(
            f"""ALTER TABLE {TABLE} ADD CONSTRAINT fiscaldocumentreview_exact_revision_fk
            FOREIGN KEY (tenant_id, client_id, document_id, revision_id)
            REFERENCES obligations_fiscalrevision (tenant_id, client_id, document_id, id);""",
            f"ALTER TABLE {TABLE} DROP CONSTRAINT fiscaldocumentreview_exact_revision_fk;",
        ),
        migrations.RunSQL(append_only_sql(TABLE), append_only_reverse_sql(TABLE)),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_rows),
    ]
