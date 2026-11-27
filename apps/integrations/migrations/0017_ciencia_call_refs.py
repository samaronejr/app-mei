"""Pin each Ciencia write and read-back to its exact approved fiscal revision."""

import django.db.models.deletion
from django.apps import registry as app_registry
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.exceptions import IrreversibleError

from apps.core.migrations._composite_fk import add_client_composite_fk


def refuse_reverse_with_calls(
    apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None
) -> None:
    """Retain the exact approved referents after any Ciencia work was queued."""
    # CROSS_TENANT_OK: any firm's queued or dispatched Ciencia call must survive.
    if (
        apps.get_model("integrations", "ProviderCall")
        ._base_manager.filter(operation__in=("FC-CIE", "FC-CIE-R"))
        .exists()
    ):
        message = "Ciencia calls exist; retain exact referents and use forward repair."
        raise IrreversibleError(message)


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0016_capture_sync"),
        ("cycles", "0024_fiscal_manifestation"),
        ("obligations", "0029_fiscal_register"),
    ]

    operations = [
        migrations.AddField(
            model_name="providercall",
            name="proposal",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                null=True,
                related_name="+",
                to="cycles.actionproposal",
            ),
        ),
        migrations.AddField(
            model_name="providercall",
            name="fiscal_revision",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                null=True,
                related_name="+",
                to="obligations.fiscalrevision",
            ),
        ),
        *[
            add_client_composite_fk(
                table="integrations_providercall",
                column=column,
                parent_table=parent,
                constraint=f"providercall_{column}_scope_fk",
            )
            for column, parent in (
                ("proposal_id", "cycles_actionproposal"),
                ("fiscal_revision_id", "obligations_fiscalrevision"),
            )
        ],
        migrations.AddConstraint(
            model_name="providercall",
            constraint=models.CheckConstraint(
                condition=models.Q(proposal__isnull=True, fiscal_revision__isnull=True)
                | models.Q(
                    operation__in=("FC-CIE", "FC-CIE-R"),
                    proposal__isnull=False,
                    fiscal_revision__isnull=False,
                ),
                name="providercall_ciencia_exact_refs",
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_calls),
    ]
