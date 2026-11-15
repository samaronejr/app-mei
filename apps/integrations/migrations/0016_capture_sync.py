"""AXRM-021: private stream positions, fenced runs and immutable acquisition work."""

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

NAMES = ("SyncStream", "SyncRun", "CaptureAcquisition")
OPTIONS = {
    "abstract": False,
    "base_manager_name": "all_objects",
    "default_manager_name": "objects",
}
STATES = ["em_fila", "executando", "concluida", "parcial", "falhou", "bloqueada"]


def scope_fields() -> list[tuple[str, models.Field[Any, Any]]]:
    """Freeze the common identity and client columns."""
    return [
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
                on_delete=django.db.models.deletion.PROTECT, to="clients.clientcompany"
            ),
        ),
        ("version", models.PositiveIntegerField(default=1)),
    ]


def managers() -> list[tuple[str, models.Manager[Any]]]:
    """Historical managers do not depend on the runtime ContextVar filter."""
    return [
        ("all_objects", django.db.models.manager.Manager()),
        ("objects", django.db.models.manager.Manager()),
    ]


def refuse_reverse_with_rows(
    apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None
) -> None:
    """Retain positions and provenance after capture starts; use forward repair."""
    for name in NAMES:
        # CROSS_TENANT_OK: reversing is refused for records in any firm's stream.
        if apps.get_model("integrations", name)._base_manager.exists():
            message = (
                "Capture rows exist; keep cursors and use flag off and forward repair."
            )
            raise IrreversibleError(message)


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0015_link_disable_epoch"),
        ("obligations", "0029_fiscal_register"),
        ("delivery", "0010_capture_sweep_cursor"),
    ]

    operations = [
        migrations.AlterField(
            model_name="integrationissue",
            name="reason",
            field=models.CharField(
                max_length=40,
                choices=[
                    ("autorizacao_ausente", "Autorização ausente"),
                    (
                        "certificado_provedor_vencendo",
                        "Certificado no provedor vencendo",
                    ),
                    ("certificado_provedor_vencido", "Certificado no provedor vencido"),
                    ("credencial_divergente", "Credencial divergente"),
                    (
                        "recebimento_reaberto_provedor",
                        "Recebimento reaberto no provedor",
                    ),
                    (
                        "chamada_provedor_incerta",
                        "Chamada ao provedor com resultado incerto",
                    ),
                    ("cota_provedor_proxima", "Cota do provedor próxima"),
                    ("cota_provedor_esgotada", "Cota do provedor esgotada"),
                    ("autenticacao_serpro_recusada", "Autenticacao SERPRO recusada"),
                    (
                        "autorizacao_procurador_ausente",
                        "Autorizacao do procurador ausente",
                    ),
                    (
                        "consulta_pagamentos_falhou",
                        "Consulta de pagamentos na Receita falhou",
                    ),
                    ("guia_das_nao_obtida", "Guia DAS oficial não obtida"),
                    ("guia_das_nao_vinculada", "Guia DAS oficial não anexada"),
                    (
                        "situacao_fiscal_nao_obtida",
                        "Relatório de situação fiscal não obtido",
                    ),
                    ("falha_na_captura", "Falha na captura de documentos fiscais"),
                ],
            ),
        ),
        migrations.CreateModel(
            name="SyncStream",
            fields=[
                *scope_fields(),
                (
                    "link",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="integrations.clientproviderlink",
                    ),
                ),
                (
                    "collection",
                    models.CharField(
                        max_length=24,
                        choices=[
                            ("nfe_recebidas", "NF-e recebidas"),
                            ("nfsen_recebidas", "NFS-e Nacional recebidas"),
                        ],
                    ),
                ),
                ("filter_digest", models.CharField(max_length=64)),
                ("epoch", models.PositiveIntegerField(default=1)),
                ("committed_cursor", models.CharField(max_length=64, default="0")),
                ("cursor_version", models.PositiveBigIntegerField(default=1)),
                ("latest_attempt_at", models.DateTimeField(null=True, blank=True)),
                ("last_api_success_at", models.DateTimeField(null=True, blank=True)),
                (
                    "last_successful_source_observation_at",
                    models.DateTimeField(null=True, blank=True),
                ),
                ("last_complete_scan_at", models.DateTimeField(null=True, blank=True)),
                ("last_error_code", models.CharField(max_length=64, blank=True)),
                ("status", models.CharField(max_length=24, default="aguardando")),
                ("consecutive_failures", models.PositiveIntegerField(default=0)),
            ],
            options={
                **OPTIONS,
                "indexes": [
                    models.Index(
                        fields=["tenant", "client", "link"], name="syncstream_scope_idx"
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "client", "id"),
                        name="syncstream_scope_id_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=(
                            "tenant",
                            "client",
                            "link",
                            "collection",
                            "filter_digest",
                            "epoch",
                        ),
                        name="syncstream_identity_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(epoch__gte=1),
                        name="syncstream_positive_epoch",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            collection__in=["nfe_recebidas", "nfsen_recebidas"]
                        ),
                        name="syncstream_known_collection",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(committed_cursor__regex=r"^[0-9]{1,64}$"),
                        name="syncstream_numeric_cursor",
                    ),
                ],
            },
            managers=managers(),
        ),
        migrations.CreateModel(
            name="SyncRun",
            fields=[
                *scope_fields(),
                (
                    "stream",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="integrations.syncstream",
                    ),
                ),
                ("occurrence_key", models.CharField(max_length=64)),
                ("trigger", models.CharField(max_length=16, default="agendada")),
                (
                    "state",
                    models.CharField(
                        max_length=16,
                        choices=[(value, value) for value in STATES],
                        default="em_fila",
                    ),
                ),
                ("start_cursor", models.CharField(max_length=64, default="0")),
                ("end_cursor", models.CharField(max_length=64, default="0")),
                ("fence", models.PositiveBigIntegerField(default=1)),
                ("lease_owner", models.UUIDField(null=True)),
                ("lease_until", models.DateTimeField(null=True)),
                ("disable_epoch", models.PositiveIntegerField(default=0)),
                ("binding_snapshot", models.JSONField(default=dict)),
                ("pages_committed", models.PositiveSmallIntegerField(default=0)),
                ("requests_made", models.PositiveSmallIntegerField(default=0)),
                ("documents_accounted", models.PositiveIntegerField(default=0)),
                ("skipped_before_start", models.PositiveIntegerField(default=0)),
                ("page_limit", models.PositiveSmallIntegerField(default=3)),
                ("request_limit", models.PositiveSmallIntegerField(default=60)),
                ("last_error_code", models.CharField(max_length=64, blank=True)),
                ("started_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("finished_at", models.DateTimeField(null=True)),
            ],
            options={
                **OPTIONS,
                "indexes": [
                    models.Index(
                        fields=["tenant", "client", "stream"], name="syncrun_scope_idx"
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "client", "id"), name="syncrun_scope_id_uniq"
                    ),
                    models.UniqueConstraint(
                        fields=("tenant", "client", "stream", "occurrence_key"),
                        name="syncrun_occurrence_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("tenant", "client", "stream"),
                        condition=models.Q(state__in=("em_fila", "executando")),
                        name="syncrun_one_active_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("tenant", "client", "stream", "id"),
                        name="syncrun_stream_id_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(state__in=STATES), name="syncrun_known_state"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(page_limit__gte=1, page_limit__lte=3),
                        name="syncrun_page_limit",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(request_limit__gte=1, request_limit__lte=60),
                        name="syncrun_request_limit",
                    ),
                ],
            },
            managers=managers(),
        ),
        migrations.CreateModel(
            name="CaptureAcquisition",
            fields=[
                *scope_fields(),
                (
                    "stream",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="integrations.syncstream",
                    ),
                ),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="integrations.syncrun",
                    ),
                ),
                (
                    "revision",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="obligations.fiscalrevision",
                    ),
                ),
                (
                    "role",
                    models.CharField(
                        max_length=16,
                        choices=[
                            ("xml_original", "xml_original"),
                            ("xml_cce", "xml_cce"),
                        ],
                    ),
                ),
                ("sequence", models.PositiveIntegerField(default=0)),
                (
                    "state",
                    models.CharField(
                        max_length=16,
                        choices=[("pendente", "pendente"), ("retida", "retida")],
                    ),
                ),
                (
                    "document",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="obligations.document",
                        null=True,
                    ),
                ),
                (
                    "call",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="integrations.providercall",
                        null=True,
                    ),
                ),
            ],
            options={
                **OPTIONS,
                "indexes": [
                    models.Index(
                        fields=["tenant", "client", "stream"],
                        name="captureacquisition_scope_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "client", "id"),
                        name="captureacquisition_scope_id_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("tenant", "client", "revision", "role", "sequence"),
                        name="captureacquisition_revision_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(state="pendente", document__isnull=True)
                        | models.Q(state="retida", document__isnull=False),
                        name="captureacquisition_retained_bytes",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(role="xml_original", sequence=0)
                        | models.Q(role="xml_cce", sequence__gt=0),
                        name="captureacquisition_known_role",
                    ),
                ],
            },
            managers=managers(),
        ),
        *[EnableRLS(name) for name in NAMES],
        *[EnablePortalClientRLS(name) for name in NAMES],
        *[DenyPortalAccess(name) for name in NAMES],
        *[
            add_tenant_composite_fk(
                table=f"integrations_{name.lower()}",
                column="client_id",
                parent_table="clients_clientcompany",
                constraint=f"{name.lower()}_client_pair_fk",
            )
            for name in NAMES
        ],
        *[
            add_client_composite_fk(
                table=f"integrations_{name}",
                column=column,
                parent_table=parent,
                constraint=f"{name}_{column}_scope_fk",
            )
            for name, column, parent in (
                ("syncstream", "link_id", "integrations_clientproviderlink"),
                ("syncrun", "stream_id", "integrations_syncstream"),
                ("captureacquisition", "stream_id", "integrations_syncstream"),
                ("captureacquisition", "run_id", "integrations_syncrun"),
                ("captureacquisition", "revision_id", "obligations_fiscalrevision"),
                ("captureacquisition", "document_id", "obligations_document"),
                ("captureacquisition", "call_id", "integrations_providercall"),
            )
        ],
        migrations.RunSQL(
            """ALTER TABLE integrations_captureacquisition ADD CONSTRAINT captureacquisition_exact_run_fk
            FOREIGN KEY (tenant_id, client_id, stream_id, run_id)
            REFERENCES integrations_syncrun (tenant_id, client_id, stream_id, id);""",
            "ALTER TABLE integrations_captureacquisition DROP CONSTRAINT captureacquisition_exact_run_fk;",
        ),
        migrations.RunSQL(
            """GRANT SELECT, INSERT, UPDATE, DELETE ON integrations_syncstream,
            integrations_syncrun TO app_runtime;""",
            "REVOKE ALL ON integrations_syncstream, integrations_syncrun FROM app_runtime;",
        ),
        migrations.RunSQL(
            append_only_sql("integrations_captureacquisition"),
            append_only_reverse_sql("integrations_captureacquisition"),
        ),
        migrations.RunSQL(
            """
            CREATE FUNCTION integrations_syncstream_guard() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF (NEW.tenant_id, NEW.client_id, NEW.link_id, NEW.collection, NEW.filter_digest, NEW.epoch)
                    IS DISTINCT FROM
                   (OLD.tenant_id, OLD.client_id, OLD.link_id, OLD.collection, OLD.filter_digest, OLD.epoch)
                THEN
                    RAISE EXCEPTION 'capture stream identity is immutable' USING ERRCODE = 'insufficient_privilege';
                END IF;
                IF NEW.committed_cursor::numeric < OLD.committed_cursor::numeric
                    OR NEW.cursor_version < OLD.cursor_version
                    OR (NEW.committed_cursor <> OLD.committed_cursor AND NEW.cursor_version <> OLD.cursor_version + 1)
                THEN
                    RAISE EXCEPTION 'capture cursor must advance with its version' USING ERRCODE = 'insufficient_privilege';
                END IF;
                RETURN NEW;
            END;
            $$;
            CREATE TRIGGER integrations_syncstream_guard BEFORE UPDATE ON integrations_syncstream
                FOR EACH ROW EXECUTE FUNCTION integrations_syncstream_guard();
            """,
            """DROP TRIGGER integrations_syncstream_guard ON integrations_syncstream;
            DROP FUNCTION integrations_syncstream_guard();""",
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_rows),
    ]
