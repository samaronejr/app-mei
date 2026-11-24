"""AXRM-049-REQUESTS: stable identities and truthful authorship for internal requests."""

import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.exceptions import IrreversibleError

# Prefer the recorded producer over a mutable label. The old oldest-title rule
# is only a fallback for requests that were already internal, never a reason
# to repurpose an ordinary namesake hidden by the preceding privacy cutover.
ADOPT_GUIDES = """
WITH candidates AS (
    SELECT r.id, r.tenant_id, r.client_id, r.cycle_id, r.created_at, r.title,
           a.metadata->>'previous_state' AS previous_state,
           (EXISTS (
               SELECT 1 FROM public.cycles_cycleevent e
               WHERE (e.tenant_id, e.client_id, e.cycle_id, e.aggregate_id) =
                     (r.tenant_id, r.client_id, r.cycle_id, r.id)
                 AND e.aggregate_type = 'EvidenceRequest'
                 AND (e.type = 'federal.guide.linked' OR
                      (e.type = 'request.created' AND e.payload->>'origin' = 'pgmei'))
           ) OR COALESCE(a.metadata->>'reason' IN
               ('guide_link_event','system_das_artifact','official_guide_observation'), false)) AS issued
    FROM public.cycles_evidencerequest r
    LEFT JOIN public.audit_event a ON a.tenant_id = r.tenant_id
      AND a.action = 'cycles.request_visibility_repaired' AND a.object_type = 'EvidenceRequest'
      AND a.object_id = r.id::text AND a.metadata->>'client_id' = r.client_id::text
      AND a.metadata->>'cycle_id' = r.cycle_id::text
    WHERE r.state = 'interno' AND NOT r.required AND r.kind = 'documento' AND r.obligation_id IS NULL
), adopted AS (
    SELECT DISTINCT ON (tenant_id, client_id, cycle_id) id FROM candidates
    WHERE issued OR (title = 'Guia DAS oficial' AND (previous_state IS NULL OR previous_state = 'interno'))
    ORDER BY tenant_id, client_id, cycle_id, issued DESC, created_at, id
)
UPDATE public.cycles_evidencerequest AS r SET purpose = 'guia_das_oficial'
FROM adopted WHERE r.id = adopted.id;
"""


def refuse_reverse_with_purposes(
    apps: Apps, schema_editor: BaseDatabaseSchemaEditor
) -> None:
    """Keep the meaning of every purposed row, even when a human created it."""
    schema_editor.execute(
        "LOCK TABLE public.cycles_evidencerequest IN ACCESS EXCLUSIVE MODE"
    )
    request = apps.get_model("cycles", "EvidenceRequest")
    # CROSS_TENANT_OK: a stored purpose in any firm requires forward repair.
    if (
        request._base_manager.using(schema_editor.connection.alias)
        .filter(purpose__isnull=False)
        .exists()
    ):
        msg = "Requests with stable purposes exist; keep them and use forward repair."
        raise IrreversibleError(msg)


class Migration(migrations.Migration):
    dependencies = [("cycles", "0027_portal_request_history")]

    operations = [
        migrations.AddField(
            model_name="evidencerequest",
            name="purpose",
            field=models.CharField(
                max_length=32,
                choices=[
                    ("guia_das_oficial", "Guia DAS oficial"),
                    ("documentos_fiscais_do_periodo", "Documentos fiscais do período"),
                    ("comprovante_municipal", "Comprovante municipal"),
                ],
                null=True,
                blank=True,
            ),
        ),
        migrations.AlterField(
            model_name="evidencerequest",
            name="created_by",
            field=models.ForeignKey(
                to=settings.AUTH_USER_MODEL,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="created_evidence_requests",
                null=True,
                blank=True,
            ),
        ),
        migrations.AddConstraint(
            model_name="evidencerequest",
            constraint=models.CheckConstraint(
                condition=models.Q(created_by__isnull=False)
                | models.Q(purpose__isnull=False),
                name="evidencerequest_human_or_purpose_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="evidencerequest",
            constraint=models.CheckConstraint(
                condition=models.Q(purpose__isnull=True)
                | (
                    models.Q(state="interno", required=False)
                    & (
                        models.Q(
                            purpose__in=[
                                "guia_das_oficial",
                                "documentos_fiscais_do_periodo",
                            ],
                            kind="documento",
                        )
                        | models.Q(purpose="comprovante_municipal", kind="comprovante")
                    )
                ),
                name="evidencerequest_internal_purpose_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="evidencerequest",
            constraint=models.UniqueConstraint(
                fields=["tenant", "client", "cycle", "purpose", "obligation"],
                condition=models.Q(purpose__isnull=False),
                nulls_distinct=False,
                name="evidencerequest_purpose_uniq",
            ),
        ),
        migrations.RunSQL(ADOPT_GUIDES, migrations.RunSQL.noop),
        # Last forwards, FIRST backwards: refuse before removing any control.
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_purposes),
    ]
