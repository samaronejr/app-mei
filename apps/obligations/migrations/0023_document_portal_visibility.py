"""AXRM-012: private originals, truthful attribution and unchanged legacy visibility."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

# INV-R10 at the policy level. The visibility test is folded into the right-hand side
# of the client comparison, as cycles 0006 does for `client_visible`: the predicate
# stays anchored on client_id (the shape tests/isolation/test_portal_rls_coverage.py
# asserts), and a hidden row compares client_id to NULL, which RLS never admits.
CLIENT = "NULLIF(current_setting('app.client_id', true), '')::uuid"
ORIGINAL = f"client_id = {CLIENT}"
VISIBLE = f"client_id = CASE WHEN portal_visible THEN {CLIENT} END"
# Quarantine metadata has no flag of its own: an intake row is READABLE exactly when
# its parent document is. The subquery runs as app_portal, so the document's tenant,
# client and portal_visible policies decide it; no second flag can drift. WITH CHECK
# keeps the client-only predicate so the composite (tenant, client, document) FK is
# still what refuses an intake naming another client's document
# (tests/isolation/test_portal_write_refusal.py); INSERT ... RETURNING still applies
# USING, and every hidden original already carries its one intake row.
PARENT_VISIBLE = (
    "EXISTS (SELECT 1 FROM obligations_document document "
    "WHERE document.id = cycles_documentintake.document_id)"
)
INTAKE_VISIBLE = f"client_id = CASE WHEN {PARENT_VISIBLE} THEN {CLIENT} END"
# Rows that REFERENCE a hidden obligation or document are hidden the same way, so the
# portal cannot read a private parent through a request, link or observation. Each
# subquery runs as app_portal under the parent's own policy; a request reaches its
# obligation, a link its document and request (and so the request's obligation), an
# observation its subject and its evidence link (and so the link's document). Only
# USING folds: the portal never writes requests or observations, and a link INSERT
# keeps the client-only WITH CHECK so the composite FKs still refuse a foreign parent.
REQUEST_OBLIGATION = (
    "EXISTS (SELECT 1 FROM obligations_obligation obligation "
    "WHERE obligation.id = cycles_evidencerequest.obligation_id)"
)
REQUEST_VISIBLE = (
    "client_id = CASE WHEN (cycles_evidencerequest.obligation_id IS NULL OR "
    f"{REQUEST_OBLIGATION}) THEN {CLIENT} END"
)
LINK_DOCUMENT = (
    "EXISTS (SELECT 1 FROM obligations_document document "
    "WHERE document.id = cycles_evidencelink.document_id)"
)
LINK_REQUEST = (
    "EXISTS (SELECT 1 FROM cycles_evidencerequest parent_request "
    "WHERE parent_request.id = cycles_evidencelink.request_id)"
)
LINK_VISIBLE = (
    f"client_id = CASE WHEN ({LINK_DOCUMENT} AND {LINK_REQUEST}) THEN {CLIENT} END"
)
OBSERVATION_SUBJECT = (
    "EXISTS (SELECT 1 FROM obligations_obligation obligation "
    "WHERE obligation.id = obligations_sourceobservation.subject_ref_id)"
)
OBSERVATION_LINK = (
    "EXISTS (SELECT 1 FROM cycles_evidencelink link "
    "WHERE link.id = obligations_sourceobservation.evidence_link_id)"
)
OBSERVATION_VISIBLE = (
    f"client_id = CASE WHEN ({OBSERVATION_SUBJECT} AND "
    f"(obligations_sourceobservation.evidence_link_id IS NULL OR {OBSERVATION_LINK})) "
    f"THEN {CLIENT} END"
)
TABLES = (
    "obligations_document",
    "obligations_obligation",
    "cycles_documentintake",
    "cycles_evidencerequest",
    "cycles_evidencelink",
    "obligations_sourceobservation",
)


def _policies(*sides: tuple[str, str]) -> str:
    return "\n".join(
        f"ALTER POLICY {table}_portal_client_isolation ON {table} "
        f"USING ({using}) WITH CHECK ({check});"
        for table, (using, check) in zip(TABLES, sides, strict=True)
    )


POLICIES = _policies(
    (VISIBLE, VISIBLE),
    (VISIBLE, VISIBLE),
    (INTAKE_VISIBLE, ORIGINAL),
    (REQUEST_VISIBLE, ORIGINAL),
    (LINK_VISIBLE, ORIGINAL),
    (OBSERVATION_VISIBLE, ORIGINAL),
)
# The pre-existing EnablePortalClientRLS predicates, restored on reverse (never dropped).
OLD_POLICIES = _policies(*[(ORIGINAL, ORIGINAL)] * len(TABLES))
TRIGGER = """
CREATE OR REPLACE FUNCTION document_portal_client_upload() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF current_user = 'app_portal' AND (
        NEW.client_upload IS DISTINCT FROM true
        OR NEW.portal_visible IS DISTINCT FROM true
        OR NEW.acquisition_origin IS DISTINCT FROM 'upload_portal'
        OR NEW.uploaded_by_id IS NULL
        OR NEW.acquisition_receipt_ref IS DISTINCT FROM ''
    ) THEN
        RAISE EXCEPTION 'document_portal_client_upload: portal writes client uploads only'
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END
$$;
"""
# This function and trigger belong to 0019. Restore its body; never delete either.
OLD_TRIGGER = """
CREATE OR REPLACE FUNCTION document_portal_client_upload() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF current_user = 'app_portal' AND NOT NEW.client_upload THEN
        RAISE EXCEPTION 'document_portal_client_upload: portal writes client uploads only'
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END
$$;
"""
BACKFILL = """
UPDATE obligations_document
SET acquisition_origin = CASE WHEN client_upload THEN 'upload_portal' ELSE 'upload_equipe' END
WHERE acquisition_origin IS DISTINCT FROM
    CASE WHEN client_upload THEN 'upload_portal' ELSE 'upload_equipe' END;
"""
# Provider attribution must survive even after a later approved publication explicitly
# makes a row visible. Every widened-hash pair contains an invisible row; refusing any
# private row also prevents exposing staff/municipal rows by dropping their predicate.
REVERSE_GUARD = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM obligations_document
        WHERE acquisition_origin IN ('captura_provedor', 'provedor_federal') OR NOT portal_visible
    )
       OR EXISTS (SELECT 1 FROM obligations_obligation WHERE NOT portal_visible) THEN
        RAISE EXCEPTION 'AXRM-012 is forward-repair-only while private originals or obligations exist';
    END IF;
END
$$;
"""
PROVIDERS = ["captura_provedor", "provedor_federal"]


class Migration(migrations.Migration):
    dependencies = [
        ("obligations", "0022_schedulerheartbeat_job_outcome"),
        # 0010 adds cycles_evidencerequest.obligation_id, which REQUEST_VISIBLE reads.
        ("cycles", "0010_receipt_binding"),
    ]

    operations = [
        migrations.AddField(
            model_name="document",
            name="portal_visible",
            field=models.BooleanField(db_default=True, default=True),
        ),
        migrations.AddField(
            model_name="obligation",
            name="portal_visible",
            field=models.BooleanField(db_default=True, default=True),
        ),
        migrations.AddField(
            model_name="document",
            name="acquisition_origin",
            field=models.CharField(
                choices=[
                    ("upload_portal", "portal upload"),
                    ("upload_equipe", "staff upload"),
                    ("captura_provedor", "provider capture"),
                    ("provedor_federal", "federal provider"),
                ],
                db_default="upload_portal",
                default="upload_portal",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="acquisition_receipt_ref",
            field=models.CharField(
                blank=True, db_default="", default="", max_length=128
            ),
        ),
        migrations.AlterField(
            model_name="document",
            name="uploaded_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="uploaded_documents",
                to=settings.AUTH_USER_MODEL,
                verbose_name="uploaded by",
            ),
        ),
        migrations.RunSQL(BACKFILL, migrations.RunSQL.noop),
        migrations.RemoveConstraint(
            model_name="document", name="document_tenant_client_sha256_uniq"
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.UniqueConstraint(
                fields=("tenant", "client", "sha256", "portal_visible"),
                name="document_tenant_client_sha256_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(uploaded_by__isnull=False)
                    | models.Q(acquisition_origin__in=PROVIDERS)
                ),
                name="document_human_or_provider_origin",
            ),
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(acquisition_origin__in=PROVIDERS)
                    | ~models.Q(acquisition_receipt_ref="")
                ),
                name="document_provider_receipt_required",
            ),
        ),
        migrations.RunSQL(POLICIES, OLD_POLICIES),
        migrations.RunSQL(TRIGGER, OLD_TRIGGER),
        # Last forwards, FIRST backwards: refuse before removing any control.
        migrations.RunSQL(migrations.RunSQL.noop, REVERSE_GUARD),
    ]
