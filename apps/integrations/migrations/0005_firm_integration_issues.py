"""AXRM-013: owned firm work, never a dummy client or a nullable client issue."""

import django.contrib.postgres.fields
import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
from django.apps import registry as app_registry
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.exceptions import IrreversibleError

import apps.core.identifiers
from apps.core.migrations._composite_fk import add_tenant_composite_fk
from apps.core.migrations._operations import DenyPortalAccess, EnableRLS


def refuse_populated_reverse(
    apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor
) -> None:
    # CROSS_TENANT_OK: every firm's recovery work must survive rollback.
    if apps.get_model("integrations", "FirmIntegrationIssue")._base_manager.exists():
        message = "Firm provider work requires forward repair."
        raise IrreversibleError(message)


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0004_provider_budgets"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.AlterField(
            model_name="integrationissue",
            name="reason",
            field=models.CharField(
                max_length=40,
                choices=[
                    ("autorizacao_ausente", "Autoriza\u00e7\u00e3o ausente"),
                    (
                        "certificado_provedor_vencendo",
                        "Certificado no provedor vencendo",
                    ),
                    ("certificado_provedor_vencido", "Certificado no provedor vencido"),
                    ("credencial_divergente", "Credencial divergente"),
                    (
                        "chamada_provedor_incerta",
                        "Chamada ao provedor com resultado incerto",
                    ),
                    ("cota_provedor_proxima", "Cota do provedor pr\u00f3xima"),
                    ("cota_provedor_esgotada", "Cota do provedor esgotada"),
                    ("autenticacao_serpro_recusada", "Autenticacao SERPRO recusada"),
                    (
                        "autorizacao_procurador_ausente",
                        "Autorizacao do procurador ausente",
                    ),
                ],
            ),
        ),
        migrations.CreateModel(
            name="FirmIntegrationIssue",
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
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="tenants.tenant",
                        verbose_name="tenant",
                    ),
                ),
                (
                    "connection",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="integrations.providerconnection",
                    ),
                ),
                (
                    "call",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="integrations.firmprovidercall",
                        null=True,
                        blank=True,
                    ),
                ),
                ("reason", models.CharField(max_length=40)),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "state",
                    models.CharField(
                        max_length=16,
                        default="aberta",
                        choices=[
                            ("aberta", "Aberta"),
                            ("em_tratamento", "Em tratamento"),
                            ("resolvida", "Resolvida"),
                        ],
                    ),
                ),
                ("review_on", models.DateField()),
                (
                    "evidence_refs",
                    django.contrib.postgres.fields.ArrayField(
                        models.CharField(max_length=64),
                        default=list,
                        blank=True,
                        size=None,
                    ),
                ),
                ("version", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                "abstract": False,
                "base_manager_name": "all_objects",
                "default_manager_name": "objects",
                "indexes": [
                    models.Index(fields=("tenant", "state"), name="firmissue_scope_idx")
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "connection", "reason"),
                        condition=models.Q(state__in=("aberta", "em_tratamento")),
                        name="firmissue_active_uniq",
                    ),
                ],
            },
            managers=[
                ("all_objects", django.db.models.manager.Manager()),
                ("objects", django.db.models.manager.Manager()),
            ],
        ),
        EnableRLS("FirmIntegrationIssue"),
        DenyPortalAccess("FirmIntegrationIssue"),
        add_tenant_composite_fk(
            table="integrations_firmintegrationissue",
            column="connection_id",
            parent_table="integrations_providerconnection",
            constraint="firmissue_connection_pair_fk",
        ),
        add_tenant_composite_fk(
            table="integrations_firmintegrationissue",
            column="call_id",
            parent_table="integrations_firmprovidercall",
            constraint="firmissue_call_pair_fk",
        ),
        migrations.RunSQL(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON integrations_firmintegrationissue TO app_runtime;",
            "REVOKE ALL ON integrations_firmintegrationissue FROM app_runtime;",
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_populated_reverse),
    ]
