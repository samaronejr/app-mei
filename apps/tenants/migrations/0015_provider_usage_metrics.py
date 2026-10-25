"""Add provider attempt metrics before the protocol writes its first receipt."""

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.exceptions import IrreversibleError

METRICS = [
    ("active_clients", "Clientes ativos"),
    ("storage_bytes", "Armazenamento em bytes"),
    ("emails_sent", "E-mails aceitos pelo servidor"),
    ("ai_shadow_calls", "Chamadas de IA em avaliação"),
    ("serpro_consulta", "Consultas SERPRO"),
    ("serpro_emissao", "Emissões SERPRO"),
    ("focus_documentos", "Documentos Focus"),
    ("focus_eventos", "Eventos Focus"),
]


def refuse_provider_receipts(apps: Apps, editor: BaseDatabaseSchemaEditor) -> None:
    """Never remove a metric that an immutable usage receipt already names."""
    model = apps.get_model("tenants", "UsageRecord")
    # CROSS_TENANT_OK: migration rollback must inspect all firms' immutable receipts.
    if (
        model._base_manager.using(editor.connection.alias)
        .filter(metric__in=[code for code, _ in METRICS[4:]])
        .exists()
    ):
        message = "Provider usage receipts require forward repair."
        raise IrreversibleError(message)


class Migration(migrations.Migration):
    dependencies = [("tenants", "0014_invite_originating_contact")]
    operations = [
        migrations.RemoveConstraint("usagerecord", "usage_known_metric_kind"),
        migrations.AlterField(
            "usagerecord", "metric", models.CharField(choices=METRICS, max_length=24)
        ),
        migrations.AddConstraint(
            "usagerecord",
            models.CheckConstraint(
                condition=models.Q(
                    metric__in=[code for code, _ in METRICS],
                    kind__in=["estimated", "confirmed"],
                ),
                name="usage_known_metric_kind",
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_provider_receipts),
    ]
