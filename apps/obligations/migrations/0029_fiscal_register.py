"""AXRM-020: client-paired fiscal identities and append-only source evidence."""

from typing import Any

import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
from django.apps import registry as app_registry
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

MODELS = ("FiscalDocument", "FiscalRevision", "RevisionArtifact")
OPTIONS = {
    "abstract": False,
    "base_manager_name": "all_objects",
    "default_manager_name": "objects",
}


def scope_fields() -> list[tuple[str, models.Field[Any, Any]]]:
    """Frozen identity columns shared by these three new tables."""
    return [
        (
            "id",
            models.UUIDField(
                default=uuid7, editable=False, primary_key=True, serialize=False
            ),
        ),
        (
            "client",
            models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="clients.clientcompany"
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
        ("version", models.PositiveIntegerField(default=1)),
    ]


def refuse_reverse_with_rows(
    apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None
) -> None:
    """Retained fiscal evidence requires flag off and forward repair (A11)."""
    for name in MODELS:
        model = apps.get_model("obligations", name)
        # CROSS_TENANT_OK: rollback is refused for evidence in any firm's register.
        if model._base_manager.exists():
            message = "Fiscal rows exist; use flag off and forward repair."
            raise IrreversibleError(message)


class Migration(migrations.Migration):
    dependencies = [
        ("clients", "0015_import_batches_and_versions"),
        ("obligations", "0028_sitfis_call_replies"),
        ("tenants", "0015_provider_usage_metrics"),
        ("audit", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="FiscalDocument",
            fields=[
                *scope_fields(),
                ("environment", models.CharField(max_length=16)),
                (
                    "family",
                    models.CharField(
                        max_length=24,
                        choices=[
                            (v, v) for v in ("nfe", "nfse_nacional", "nfse_municipal")
                        ],
                    ),
                ),
                ("key_namespace", models.CharField(max_length=64)),
                ("document_key", models.CharField(max_length=128)),
                (
                    "client_role",
                    models.CharField(
                        max_length=16,
                        choices=[
                            (v, v)
                            for v in (
                                "destinatario",
                                "emitente",
                                "tomador",
                                "prestador",
                            )
                        ],
                    ),
                ),
                ("counterpart_id", models.CharField(max_length=14, blank=True)),
                ("counterpart_name", models.CharField(max_length=255, blank=True)),
                ("issue_date", models.DateField(null=True)),
                ("competence", models.DateField(null=True)),
                ("municipality_incidence", models.CharField(max_length=7, blank=True)),
                (
                    "total",
                    models.DecimalField(max_digits=14, decimal_places=2, null=True),
                ),
                (
                    "iss_stated",
                    models.DecimalField(max_digits=14, decimal_places=2, null=True),
                ),
                ("iss_withheld_flag", models.BooleanField(null=True)),
                (
                    "iss_withheld_amount",
                    models.DecimalField(max_digits=14, decimal_places=2, null=True),
                ),
                ("summary_only", models.BooleanField(default=True)),
                (
                    "source",
                    models.CharField(
                        max_length=24,
                        choices=[
                            (v, v)
                            for v in ("captura", "upload_cliente", "importacao_equipe")
                        ],
                    ),
                ),
                ("provider", models.CharField(max_length=32, blank=True)),
                (
                    "fiscal_state",
                    models.CharField(max_length=16, default="desconhecida"),
                ),
                (
                    "role_verification",
                    models.CharField(
                        max_length=16,
                        default="unconfirmed",
                        choices=[
                            (v, v) for v in ("unconfirmed", "confirmed", "divergent")
                        ],
                    ),
                ),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options=OPTIONS,
            managers=[
                ("all_objects", django.db.models.manager.Manager()),
                ("objects", django.db.models.manager.Manager()),
            ],
        ),
        migrations.CreateModel(
            name="FiscalRevision",
            fields=[
                *scope_fields(),
                (
                    "document",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="revisions",
                        to="obligations.fiscaldocument",
                    ),
                ),
                ("source_namespace", models.CharField(max_length=64)),
                ("provider_version", models.CharField(max_length=64)),
                ("semantic_digest", models.CharField(max_length=64)),
                (
                    "fiscal_state",
                    models.CharField(
                        max_length=16,
                        choices=[
                            (v, v)
                            for v in (
                                "autorizada",
                                "cancelada",
                                "substituida",
                                "denegada",
                                "desconhecida",
                            )
                        ],
                    ),
                ),
                ("source_effective_at", models.DateTimeField(null=True)),
                (
                    "observed_at",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                ("replacement_key", models.CharField(max_length=128, blank=True)),
                ("receipt_ref", models.CharField(max_length=128, blank=True)),
                ("provenance", models.JSONField(default=dict)),
            ],
            options=OPTIONS,
            managers=[
                ("all_objects", django.db.models.manager.Manager()),
                ("objects", django.db.models.manager.Manager()),
            ],
        ),
        migrations.AddField(
            model_name="fiscaldocument",
            name="current_revision",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="obligations.fiscalrevision",
            ),
        ),
        migrations.CreateModel(
            name="RevisionArtifact",
            fields=[
                *scope_fields(),
                (
                    "revision",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="artifacts",
                        to="obligations.fiscalrevision",
                    ),
                ),
                (
                    "role",
                    models.CharField(
                        max_length=16,
                        choices=[
                            (v, v) for v in ("resumo", "xml_original", "xml_cce", "pdf")
                        ],
                    ),
                ),
                (
                    "document",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="fiscal_artifacts",
                        to="obligations.document",
                    ),
                ),
            ],
            options=OPTIONS,
            managers=[
                ("all_objects", django.db.models.manager.Manager()),
                ("objects", django.db.models.manager.Manager()),
            ],
        ),
        *[
            migrations.AddConstraint(
                model_name=name.lower(),
                constraint=models.UniqueConstraint(
                    fields=["tenant", "client", "id"],
                    name=f"{name.lower()}_scope_id_uniq",
                ),
            )
            for name in MODELS
        ],
        migrations.AddConstraint(
            model_name="fiscaldocument",
            constraint=models.UniqueConstraint(
                fields=[
                    "tenant",
                    "client",
                    "environment",
                    "family",
                    "key_namespace",
                    "document_key",
                ],
                name="fiscaldocument_identity_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="fiscalrevision",
            constraint=models.UniqueConstraint(
                fields=[
                    "tenant",
                    "client",
                    "document",
                    "source_namespace",
                    "provider_version",
                ],
                name="fiscalrevision_source_version_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="revisionartifact",
            constraint=models.UniqueConstraint(
                fields=["tenant", "client", "revision", "role", "document"],
                name="revisionartifact_bytes_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="fiscalrevision",
            constraint=models.UniqueConstraint(
                fields=["tenant", "client", "document", "id"],
                name="fiscalrevision_document_id_uniq",
            ),
        ),
        migrations.AddIndex(
            model_name="fiscaldocument",
            index=models.Index(
                fields=["tenant", "client", "competence"],
                name="fiscaldocument_period_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="fiscalrevision",
            index=models.Index(
                fields=["tenant", "client", "document"], name="fiscalrevision_doc_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="revisionartifact",
            index=models.Index(
                fields=["tenant", "client", "revision"], name="revisionartifact_rev_idx"
            ),
        ),
        *[EnableRLS(name) for name in MODELS],
        *[EnablePortalClientRLS(name) for name in MODELS],
        *[DenyPortalAccess(name) for name in MODELS],
        *[
            add_tenant_composite_fk(
                table=f"obligations_{name.lower()}",
                column="client_id",
                parent_table="clients_clientcompany",
                constraint=f"{name.lower()}_client_pair_fk",
            )
            for name in MODELS
        ],
        *[
            add_client_composite_fk(
                table=f"obligations_{name}",
                column=column,
                parent_table=f"obligations_{parent}",
                constraint=f"{name}_{column}_scope_fk",
            )
            for name, column, parent in (
                ("fiscaldocument", "current_revision_id", "fiscalrevision"),
                ("fiscalrevision", "document_id", "fiscaldocument"),
                ("revisionartifact", "revision_id", "fiscalrevision"),
                ("revisionartifact", "document_id", "document"),
            )
        ],
        migrations.RunSQL(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON obligations_fiscaldocument TO app_runtime;",
            "REVOKE ALL ON obligations_fiscaldocument FROM app_runtime;",
        ),
        *[
            migrations.RunSQL(append_only_sql(table), append_only_reverse_sql(table))
            for table in ("obligations_fiscalrevision", "obligations_revisionartifact")
        ],
        migrations.RunSQL(
            """
            ALTER TABLE obligations_fiscaldocument
                ADD CONSTRAINT fiscaldocument_exact_head_fk
                FOREIGN KEY (tenant_id, client_id, id, current_revision_id)
                REFERENCES obligations_fiscalrevision
                    (tenant_id, client_id, document_id, id);
            """,
            """
            ALTER TABLE obligations_fiscaldocument
                DROP CONSTRAINT fiscaldocument_exact_head_fk;
            """,
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_rows),
    ]
