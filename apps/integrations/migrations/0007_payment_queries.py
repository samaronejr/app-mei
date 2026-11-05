"""AXRM-015: PAGTOWEB consultations, firm-only and client-paired (A7.1)."""

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
from apps.core.migrations._composite_fk import (
    add_client_composite_fk,
    add_tenant_composite_fk,
)
from apps.core.migrations._operations import (
    DenyPortalAccess,
    EnablePortalClientRLS,
    EnableRLS,
)


def refuse_populated_reverse(
    apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor
) -> None:
    # CROSS_TENANT_OK: no firm's consultation history may be dropped by a rollback.
    if apps.get_model("integrations", "PaymentQuery")._base_manager.exists():
        message = "PAGTOWEB consultations require forward repair, not reversal."
        raise IrreversibleError(message)


class Migration(migrations.Migration):
    dependencies = [
        ("clients", "0015_import_batches_and_versions"),
        ("integrations", "0006_firm_provider_reservations"),
        ("tenants", "0015_provider_usage_metrics"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="integrationissue",
            name="reason",
            field=models.CharField(
                choices=[
                    ("autorizacao_ausente", "Autorização ausente"),
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
                ],
                max_length=40,
            ),
        ),
        migrations.CreateModel(
            name="PaymentQuery",
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
                    "trigger",
                    models.CharField(
                        choices=[("equipe", "Equipe"), ("agendada", "Agendada")],
                        default="agendada",
                        max_length=16,
                    ),
                ),
                ("request_key", models.CharField(max_length=64)),
                ("range_start", models.DateField()),
                ("range_end", models.DateField()),
                (
                    "revenue_codes",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(max_length=4),
                        blank=True,
                        default=list,
                        size=None,
                    ),
                ),
                ("environment", models.CharField(max_length=16)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("na_fila", "Na fila"),
                            ("em_andamento", "Em andamento"),
                            ("encontrado", "Pagamentos encontrados"),
                            ("nada_localizado", "Nada localizado"),
                            ("sem_procuracao", "Sem procuração"),
                            ("falha", "Falha"),
                            ("cota_esgotada", "Limite de consultas atingido"),
                            ("incerto", "Incerto"),
                        ],
                        default="na_fila",
                        max_length=24,
                    ),
                ),
                ("pages_fetched", models.PositiveSmallIntegerField(default=0)),
                ("payments_found", models.PositiveIntegerField(default=0)),
                ("matched_count", models.PositiveIntegerField(default=0)),
                (
                    "failure_code",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("version", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="clients.clientcompany",
                    ),
                ),
                (
                    "link",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="integrations.clientproviderlink",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
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
                        fields=["tenant", "client"], name="paymentquery_scope_idx"
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "client", "id"),
                        name="paymentquery_scope_id_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("tenant", "client", "request_key"),
                        name="paymentquery_request_uniq",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("state__in", ("na_fila", "em_andamento"))),
                        fields=("tenant", "client"),
                        name="paymentquery_one_active",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            (
                                "state__in",
                                [
                                    "na_fila",
                                    "em_andamento",
                                    "encontrado",
                                    "nada_localizado",
                                    "sem_procuracao",
                                    "falha",
                                    "cota_esgotada",
                                    "incerto",
                                ],
                            )
                        ),
                        name="paymentquery_known_state",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                ("requested_by__isnull", False), ("trigger", "equipe")
                            ),
                            models.Q(
                                ("requested_by__isnull", True), ("trigger", "agendada")
                            ),
                            _connector="OR",
                        ),
                        name="paymentquery_trigger_actor",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("range_end__gte", models.F("range_start"))),
                        name="paymentquery_range_order",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("pages_fetched__lte", 10)),
                        name="paymentquery_page_cap",
                    ),
                ],
            },
            managers=[
                ("all_objects", django.db.models.manager.Manager()),
                ("objects", django.db.models.manager.Manager()),
            ],
        ),
        EnableRLS("PaymentQuery"),
        EnablePortalClientRLS("PaymentQuery"),
        DenyPortalAccess("PaymentQuery"),
        add_tenant_composite_fk(
            table="integrations_paymentquery",
            column="client_id",
            parent_table="clients_clientcompany",
            constraint="paymentquery_client_fk",
        ),
        add_client_composite_fk(
            table="integrations_paymentquery",
            column="link_id",
            parent_table="integrations_clientproviderlink",
            constraint="paymentquery_link_fk",
        ),
        migrations.RunSQL(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON integrations_paymentquery "
            "TO app_runtime;",
            "REVOKE ALL ON integrations_paymentquery FROM app_runtime;",
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_populated_reverse),
    ]
