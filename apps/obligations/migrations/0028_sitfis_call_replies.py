"""AXRM-016: retain each call's receipts independently of report progress."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError

if TYPE_CHECKING:
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.migrations.state import StateApps


def refuse_receipt_loss(
    apps: StateApps, _schema_editor: BaseDatabaseSchemaEditor
) -> None:
    """Refuse reversal while a call has retained a durable reply."""
    report = apps.get_model("obligations", "FiscalSituationReport")
    # CROSS_TENANT_OK: a schema rollback must not discard any firm's paid receipts.
    if report._base_manager.exclude(call_replies={}).exists():
        message = "SITFIS receipts require forward repair."
        raise IrreversibleError(message)


class Migration(migrations.Migration):
    dependencies = [
        ("obligations", "0027_sitfis_reply_record"),
    ]

    operations = [
        migrations.AddField(
            model_name="fiscalsituationreport",
            name="call_replies",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_receipt_loss),
    ]
