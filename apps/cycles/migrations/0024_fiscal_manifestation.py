"""Extend exact R1 proposal and execution kinds without changing old manifests."""

from django.apps import registry as app_registry
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.exceptions import IrreversibleError


def refuse_reverse_with_batches(
    apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None
) -> None:
    """Preserve frozen client approvals once Ciencia proposals exist."""
    # CROSS_TENANT_OK: any firm's immutable fiscal proposal forbids this rollback.
    if (
        apps.get_model("cycles", "ActionProposal")
        ._base_manager.filter(action_kind="fiscal_manifestation")
        .exists()
    ):
        message = "Ciencia batches exist; retain approvals and use forward repair."
        raise IrreversibleError(message)


class Migration(migrations.Migration):
    dependencies = [("cycles", "0023_fiscal_document_review")]

    operations = [
        migrations.AlterField(
            model_name="actionproposal",
            name="action_kind",
            field=models.CharField(
                max_length=32,
                choices=[
                    ("client_publication", "Publicar documentos"),
                    ("email_notice", "Enviar e-mail"),
                    ("cycle_completion", "Concluir ciclo"),
                    ("request_waiver", "Dispensar solicita\u00e7\u00e3o"),
                    ("fiscal_manifestation", "Registrar ciencia (lote)"),
                ],
            ),
        ),
        migrations.RemoveConstraint(
            model_name="actionexecution", name="execution_known_kind"
        ),
        migrations.AddConstraint(
            model_name="actionexecution",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    action_kind__in=[
                        "client_publication",
                        "email_notice",
                        "cycle_completion",
                        "request_waiver",
                        "fiscal_manifestation",
                    ]
                ),
                name="execution_known_kind",
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_batches),
    ]
