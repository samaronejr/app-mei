"""Retain the private keys, authorization dates, and deadlines of Ciencia batches."""

from django.apps import registry as app_registry
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.exceptions import IrreversibleError


def refuse_reverse_with_followups(
    apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None
) -> None:
    """Never discard a professional's registered manifestation follow-up."""
    # CROSS_TENANT_OK: retained fiscal follow-up data in any firm forbids rollback.
    if (
        apps.get_model("cycles", "ExceptionInternal")
        ._base_manager.exclude(manifestation_documents=[])
        .exists()
    ):
        message = "Manifestation follow-ups exist; retain them and use forward repair."
        raise IrreversibleError(message)


class Migration(migrations.Migration):
    dependencies = [("cycles", "0024_fiscal_manifestation")]

    operations = [
        migrations.AddField(
            model_name="exceptioninternal",
            name="manifestation_documents",
            field=models.JSONField(default=list),
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_followups),
    ]
