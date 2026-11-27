"""Keep the per-key identity and owned duty independently of a lost approval."""

from typing import Any

import django.db.models.deletion
import django.utils.timezone
from django.apps import registry as app_registry
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.exceptions import IrreversibleError

from apps.core.identifiers import uuid7
from apps.core.migrations._composite_fk import (
    add_client_composite_fk,
    add_client_parent_unique,
    add_tenant_composite_fk,
)
from apps.core.migrations._operations import (
    DenyPortalAccess,
    EnablePortalClientRLS,
    EnableRLS,
)


def refuse_reverse_with_records(
    apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None
) -> None:
    # CROSS_TENANT_OK: every firm's registered target and duty must survive.
    if apps.get_model("integrations", "CienciaRecord")._base_manager.exists():
        message = "Ciencia recovery records exist; retain them and use forward repair."
        raise IrreversibleError(message)


ISSUE_RETAINED = """
CREATE FUNCTION cienciarecord_issue_retained() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM integrations_cienciarecord WHERE issue_id = OLD.id
    ) THEN
        RAISE EXCEPTION 'a Ciencia recovery duty still references this issue'
            USING ERRCODE = '23503';
    END IF;
    RETURN OLD;
END;
$$;
CREATE TRIGGER cienciarecord_issue_retained
BEFORE DELETE ON integrations_integrationissue
FOR EACH ROW EXECUTE FUNCTION cienciarecord_issue_retained();
"""

ISSUE_RETAINED_REVERSE = """
DROP TRIGGER cienciarecord_issue_retained ON integrations_integrationissue;
DROP FUNCTION cienciarecord_issue_retained();
"""


def parent(target: str, *, nullable: bool = False) -> models.Field[Any, Any]:
    return models.ForeignKey(
        to=target,
        on_delete=django.db.models.deletion.PROTECT,
        related_name="+",
        null=nullable,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0019_ciencia_reference_constraint"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
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
                    (
                        "certificado_provedor_vencido",
                        "Certificado no provedor vencido",
                    ),
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
                    (
                        "manifestacao_conclusiva_pendente",
                        "Manifestação conclusiva pendente",
                    ),
                ],
            ),
        ),
        migrations.CreateModel(
            name="CienciaRecord",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid7,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        to="tenants.tenant",
                        on_delete=django.db.models.deletion.PROTECT,
                        verbose_name="tenant",
                    ),
                ),
                ("client", parent("clients.clientcompany")),
                ("call", parent("integrations.providercall")),
                ("read_call", parent("integrations.providercall", nullable=True)),
                ("environment", models.CharField(max_length=16)),
                ("document_key", models.CharField(max_length=44)),
                ("proposal_ref", models.UUIDField()),
                ("cycle_ref", models.UUIDField()),
                ("fiscal_revision_ref", models.UUIDField()),
                ("engagement_ref", models.UUIDField()),
                ("owner", parent(settings.AUTH_USER_MODEL)),
                ("issued_on", models.DateField()),
                ("authorized_on", models.DateField(null=True)),
                ("annex_ii", models.BooleanField(default=False)),
                ("confirmed_at", models.DateTimeField(null=True)),
                ("verified_at", models.DateTimeField(null=True)),
                ("read_sequence", models.PositiveIntegerField(default=0)),
                ("cycle_exception", parent("cycles.exception", nullable=True)),
                ("issue", parent("integrations.integrationissue", nullable=True)),
                ("resolution", models.JSONField(null=True)),
                ("resolved_by", parent(settings.AUTH_USER_MODEL, nullable=True)),
                ("resolved_at", models.DateTimeField(null=True)),
                ("recovered", models.BooleanField(default=False)),
                ("version", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                "abstract": False,
                "base_manager_name": "all_objects",
                "default_manager_name": "objects",
                "indexes": [
                    models.Index(
                        fields=["tenant", "client"], name="cienciarecord_scope_idx"
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "client", "id"),
                        name="cienciarecord_scope_id_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("tenant", "client", "call"),
                        name="cienciarecord_call_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("tenant", "client", "environment", "document_key"),
                        name="cienciarecord_key_uniq",
                    ),
                ],
            },
            managers=[
                ("all_objects", models.Manager()),
                ("objects", models.Manager()),
            ],
        ),
        EnableRLS("CienciaRecord"),
        EnablePortalClientRLS("CienciaRecord"),
        DenyPortalAccess("CienciaRecord"),
        add_tenant_composite_fk(
            table="integrations_cienciarecord",
            column="client_id",
            parent_table="clients_clientcompany",
            constraint="cienciarecord_client_pair_fk",
        ),
        add_client_parent_unique(
            table="integrations_integrationissue",
            constraint="integrationissue_scope_id_uniq",
        ),
        *[
            add_client_composite_fk(
                table="integrations_cienciarecord",
                column=column,
                parent_table=target,
                constraint=f"cienciarecord_{column}_scope_fk",
            )
            for column, target in (
                ("call_id", "integrations_providercall"),
                ("read_call_id", "integrations_providercall"),
                ("cycle_exception_id", "cycles_exception"),
                ("issue_id", "integrations_integrationissue"),
            )
        ],
        migrations.RunSQL(
            "GRANT SELECT, INSERT, UPDATE ON integrations_cienciarecord TO app_runtime; "
            "REVOKE DELETE ON integrations_cienciarecord FROM app_runtime;",
            "REVOKE ALL ON integrations_cienciarecord FROM app_runtime;",
        ),
        # AXRM-022 (deliberate, kept): the composite issue FK cascades; a referenced
        # issue cannot be deleted from under its recovery duty.
        migrations.RunSQL(ISSUE_RETAINED, ISSUE_RETAINED_REVERSE),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_records),
    ]
