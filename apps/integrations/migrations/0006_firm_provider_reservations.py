"""AXRM-013: explicit zero-price admission for each client-less SP-TERMO attempt."""

import django.db.models.deletion
import django.db.models.manager
from django.apps import registry as app_registry
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.exceptions import IrreversibleError

import apps.core.identifiers
from apps.core.migrations._composite_fk import add_tenant_composite_fk
from apps.core.migrations._operations import DenyPortalAccess, EnableRLS


def refuse_populated_reverse(
    apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor
) -> None:
    # CROSS_TENANT_OK: even zero-price admission history belongs to every firm.
    if apps.get_model("integrations", "FirmProviderReservation")._base_manager.exists():
        message = "Firm provider reservations require forward repair."
        raise IrreversibleError(message)


class Migration(migrations.Migration):
    dependencies = [("integrations", "0005_firm_integration_issues")]
    operations = [
        migrations.CreateModel(
            name="FirmProviderReservation",
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
                    "call",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="integrations.firmprovidercall",
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
                ("attempt_number", models.PositiveIntegerField()),
                ("microbrl", models.PositiveBigIntegerField(default=0)),
                (
                    "tariff_revision",
                    models.CharField(
                        max_length=64,
                        default="serpro-free-v1",
                    ),
                ),
                (
                    "state",
                    models.CharField(
                        max_length=16,
                        default="reserved",
                        choices=[("reserved", "Reserved"), ("released", "Released")],
                    ),
                ),
                ("version", models.PositiveIntegerField(default=1)),
            ],
            options={
                "abstract": False,
                "base_manager_name": "all_objects",
                "default_manager_name": "objects",
                "indexes": [
                    models.Index(
                        fields=("tenant", "call"), name="firmreservation_scope_idx"
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "call", "attempt_number"),
                        name="firmreservation_attempt_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(microbrl=0), name="firmreservation_is_free"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(state__in=("reserved", "released")),
                        name="firmreservation_known_state",
                    ),
                ],
            },
            managers=[
                ("all_objects", django.db.models.manager.Manager()),
                ("objects", django.db.models.manager.Manager()),
            ],
        ),
        EnableRLS("FirmProviderReservation"),
        DenyPortalAccess("FirmProviderReservation"),
        add_tenant_composite_fk(
            table="integrations_firmproviderreservation",
            column="call_id",
            parent_table="integrations_firmprovidercall",
            constraint="firmreservation_call_pair_fk",
        ),
        add_tenant_composite_fk(
            table="integrations_firmproviderreservation",
            column="budget_id",
            parent_table="integrations_providerbudget",
            constraint="firmreservation_budget_pair_fk",
        ),
        migrations.RunSQL(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON integrations_firmproviderreservation TO app_runtime;",
            "REVOKE ALL ON integrations_firmproviderreservation FROM app_runtime;",
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_populated_reverse),
    ]
