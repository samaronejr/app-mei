"""AXRM-046: corroborate legacy ISS party outside the column-add transaction."""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

from apps.obligations.iss_retention_backfill_v1 import backfill_iss_retention_party


def backfill(_apps: Apps, editor: BaseDatabaseSchemaEditor) -> None:
    backfill_iss_retention_party(editor.connection)


class Migration(migrations.Migration):
    atomic = False
    dependencies = [
        ("audit", "0008_fiscal_event_order"),
        ("cycles", "0011_actionproposal_redacted_at_and_more"),
        ("obligations", "0030_iss_retention_party"),
    ]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
