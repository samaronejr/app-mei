"""AXRM-014 repair: durable consultation scopes and bidirectional report privacy."""

import hashlib
import json

from django.apps import registry as app_registry
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.exceptions import IrreversibleError

PRIVATE_DOCUMENT_FK = """
ALTER TABLE obligations_fiscalsituationreport
    ADD CONSTRAINT sitfisreport_document_private_fk
    FOREIGN KEY (tenant_id, client_id, document_id, document_portal_visible)
    REFERENCES obligations_document (tenant_id, client_id, id, portal_visible);
"""
DROP_PRIVATE_DOCUMENT_FK = """
ALTER TABLE obligations_fiscalsituationreport
    DROP CONSTRAINT sitfisreport_document_private_fk;
"""


def backfill_recorded_scope(
    apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor
) -> None:
    """Hash only the legacy query scope actually retained, not an invented request.

    Legacy rows did not retain the provider's canonical request digest. Their
    backfill is explicitly namespaced and cannot masquerade as that digest.
    """
    consultation = apps.get_model("obligations", "FederalConsultation")
    # CROSS_TENANT_OK: migration authority backfills every firm's existing receipt.
    for row in consultation._base_manager.filter(scope_digest="").iterator():
        scope = {
            "contract": "axrm014-legacy-recorded-scope-v1",
            "tenant": str(row.tenant_id),
            "client": str(row.client_id),
            "kind": row.kind,
            "environment": row.environment,
            "range_start": row.range_start.isoformat() if row.range_start else None,
            "range_end": row.range_end.isoformat() if row.range_end else None,
        }
        row.scope_digest = hashlib.sha256(
            json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        row.save(update_fields=["scope_digest"])


def refuse_populated_reverse(
    apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor
) -> None:
    """Keep recorded scopes and privacy protection once federal receipts exist."""
    consultation = apps.get_model("obligations", "FederalConsultation")
    # CROSS_TENANT_OK: the reverse must inspect every firm's durable evidence.
    if consultation._base_manager.exists():
        msg = (
            "obligations.0025 retains consultation scopes and report privacy: "
            "use forward repair while federal receipts exist."
        )
        raise IrreversibleError(msg)


class Migration(migrations.Migration):
    dependencies = [("obligations", "0024_federal_records")]

    operations = [
        migrations.AddField(
            model_name="federalconsultation",
            name="scope_digest",
            field=models.CharField(default="", max_length=64),
            preserve_default=False,
        ),
        migrations.RunPython(backfill_recorded_scope, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="federalconsultation",
            constraint=models.CheckConstraint(
                condition=models.Q(scope_digest__regex="^[a-f0-9]{64}$"),
                name="federalconsultation_scope_digest",
            ),
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.UniqueConstraint(
                fields=("tenant", "client", "id", "portal_visible"),
                name="document_client_visibility_uniq",
            ),
        ),
        migrations.AddField(
            model_name="fiscalsituationreport",
            name="document_portal_visible",
            field=models.GeneratedField(
                expression=models.Value(value=False),
                output_field=models.BooleanField(),
                db_persist=True,
            ),
        ),
        migrations.RunSQL(PRIVATE_DOCUMENT_FK, DROP_PRIVATE_DOCUMENT_FK),
        migrations.RunPython(migrations.RunPython.noop, refuse_populated_reverse),
    ]
