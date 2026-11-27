"""AXRM-022-SCAFFOLD: no fiscal proposal or execution marker can be stored yet.

22-06 removes the proposal check and 22-07 the execution check, each with a populated
reverse guard. An existing fiscal row stops this migration; nothing is rewritten.
"""

from django.apps import registry as app_registry
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

FISCAL = "fiscal_manifestation"


class FiscalRowsExistError(Exception):
    """A stored fiscal row the closure would otherwise fail on, raw or silently."""


def refuse_existing_fiscal_rows(
    apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None
) -> None:
    """Stop before the checks: existing rows are retained, never rewritten."""
    # CROSS_TENANT_OK: any firm's stored fiscal row forbids closing the kind.
    if (
        apps.get_model("cycles", "ActionProposal")
        ._base_manager.filter(action_kind=FISCAL)
        .exists()
        or apps.get_model("cycles", "ActionExecution")
        ._base_manager.filter(action_kind=FISCAL)
        .exists()
    ):
        message = (
            "Fiscal manifestation rows exist; cycles 0026 cannot close the kind. "
            "Retain them and use forward repair."
        )
        raise FiscalRowsExistError(message)


class Migration(migrations.Migration):
    dependencies = [("cycles", "0025_manifestation_followup")]

    operations = [
        migrations.RunPython(refuse_existing_fiscal_rows, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="actionproposal",
            constraint=models.CheckConstraint(
                condition=~models.Q(action_kind=FISCAL),
                name="actionproposal_fiscal_closed",
            ),
        ),
        migrations.AddConstraint(
            model_name="actionexecution",
            constraint=models.CheckConstraint(
                condition=~models.Q(action_kind=FISCAL),
                name="actionexecution_fiscal_closed",
            ),
        ),
    ]
