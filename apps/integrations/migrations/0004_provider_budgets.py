"""AXRM-010: monthly caps, client-private attempt balances and quota issue reasons."""

import django.db.models.deletion
import django.db.models.manager
from django.apps import registry as app_registry
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
    for name in ("ProviderReservation", "ProviderBudget"):
        # CROSS_TENANT_OK: rollback protects every firm's financial history.
        if apps.get_model("integrations", name)._base_manager.exists():
            message = "Provider balances require forward repair."
            raise IrreversibleError(message)


class Migration(migrations.Migration):
    dependencies = [("integrations", "0003_call_protocol")]

    operations = [
        # Choices only (no SQL): quota work reuses AXRM-008's IntegrationIssue.
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
                ],
                max_length=40,
            ),
        ),
        migrations.CreateModel(
            name="ProviderBudget",
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
                    "provider",
                    models.CharField(
                        choices=[("serpro", "SERPRO"), ("focus", "Focus")],
                        max_length=16,
                    ),
                ),
                ("window_start", models.DateField()),
                ("cap_microbrl", models.PositiveBigIntegerField()),
                ("known_liability", models.PositiveBigIntegerField(default=0)),
                ("uncertain_exposure", models.PositiveBigIntegerField(default=0)),
                ("reserved", models.PositiveBigIntegerField(default=0)),
                ("tariff_revision", models.CharField(max_length=64)),
                ("included_documents", models.PositiveIntegerField(default=0)),
                ("version", models.PositiveIntegerField(default=1)),
            ],
            options={
                "abstract": False,
                "base_manager_name": "all_objects",
                "default_manager_name": "objects",
                "indexes": [
                    models.Index(
                        fields=("tenant", "provider"), name="providerbudget_scope_idx"
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "provider", "window_start"),
                        name="providerbudget_window_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("tenant", "id"), name="providerbudget_tenant_id_uniq"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(window_start__day=1),
                        name="providerbudget_month_start",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(provider__in=("serpro", "focus")),
                        name="providerbudget_known_provider",
                    ),
                ],
            },
            managers=[
                ("all_objects", django.db.models.manager.Manager()),
                ("objects", django.db.models.manager.Manager()),
            ],
        ),
        migrations.CreateModel(
            name="ProviderReservation",
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
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="clients.clientcompany",
                    ),
                ),
                (
                    "budget",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="integrations.providerbudget",
                    ),
                ),
                (
                    "call",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="integrations.providercall",
                    ),
                ),
                ("attempt_number", models.PositiveIntegerField(default=1)),
                ("microbrl", models.PositiveBigIntegerField(default=0)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("reserved", "Reserved"),
                            ("known", "Known"),
                            ("uncertain", "Uncertain"),
                            ("released", "Released"),
                        ],
                        default="reserved",
                        max_length=16,
                    ),
                ),
                ("tariff_revision", models.CharField(max_length=64)),
                ("version", models.PositiveIntegerField(default=1)),
            ],
            options={
                "abstract": False,
                "base_manager_name": "all_objects",
                "default_manager_name": "objects",
                "indexes": [
                    models.Index(
                        fields=("tenant", "client"),
                        name="providerreservation_scope_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "client", "id"),
                        name="providerreservation_scope_id_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("tenant", "client", "call", "attempt_number"),
                        name="providerreservation_attempt_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            state__in=("reserved", "known", "uncertain", "released")
                        ),
                        name="providerreservation_known_state",
                    ),
                ],
            },
            managers=[
                ("all_objects", django.db.models.manager.Manager()),
                ("objects", django.db.models.manager.Manager()),
            ],
        ),
        EnableRLS("ProviderBudget"),
        DenyPortalAccess("ProviderBudget"),
        EnableRLS("ProviderReservation"),
        EnablePortalClientRLS("ProviderReservation"),
        DenyPortalAccess("ProviderReservation"),
        add_tenant_composite_fk(
            table="integrations_providerreservation",
            column="client_id",
            parent_table="clients_clientcompany",
            constraint="providerreservation_client_fk",
        ),
        add_tenant_composite_fk(
            table="integrations_providerreservation",
            column="budget_id",
            parent_table="integrations_providerbudget",
            constraint="providerreservation_budget_fk",
        ),
        add_client_composite_fk(
            table="integrations_providerreservation",
            column="call_id",
            parent_table="integrations_providercall",
            constraint="providerreservation_call_fk",
        ),
        migrations.RunSQL(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON integrations_providerbudget, "
            "integrations_providerreservation TO app_runtime;",
            "REVOKE ALL ON integrations_providerbudget, "
            "integrations_providerreservation FROM app_runtime;",
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_populated_reverse),
    ]
