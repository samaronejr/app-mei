"""Keep the client's Annex II classification beside its append-only authority."""

from django.apps import registry as app_registry
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.exceptions import IrreversibleError


def refuse_reverse_with_notes(
    apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None
) -> None:
    """Never discard a recorded client-specific Annex II assessment."""
    # CROSS_TENANT_OK: a professional assessment in any firm must survive rollback.
    records = apps.get_model("integrations", "AuthorityRecord")._base_manager
    if records.filter(conclusive_manifestation_annex_ii=True).exists() or (
        records.exclude(conclusive_manifestation_note="").exists()
    ):
        message = (
            "Ciencia professional notes exist; retain them and use forward repair."
        )
        raise IrreversibleError(message)


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0017_ciencia_call_refs"),
        ("cycles", "0025_manifestation_followup"),
    ]

    operations = [
        migrations.AddField(
            model_name="authorityrecord",
            name="conclusive_manifestation_annex_ii",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="authorityrecord",
            name="conclusive_manifestation_note",
            field=models.TextField(blank=True, max_length=2000),
        ),
        migrations.AddConstraint(
            model_name="authorityrecord",
            constraint=models.CheckConstraint(
                condition=models.Q(conclusive_manifestation_annex_ii=False)
                | (
                    models.Q(service="ciencia")
                    & ~models.Q(conclusive_manifestation_note="")
                ),
                name="authorityrecord_conclusive_note",
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_notes),
    ]
