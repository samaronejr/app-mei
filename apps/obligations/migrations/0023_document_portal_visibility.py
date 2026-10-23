"""AXRM-012: private originals, truthful attribution and unchanged legacy visibility."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

CLIENT = "client_id = NULLIF(current_setting('app.client_id', true), '')::uuid"
TABLES = ("obligations_document", "obligations_obligation")
POLICIES = "\n".join(
    f"ALTER POLICY {table}_portal_client_isolation ON {table} "
    f"USING ({CLIENT} AND portal_visible) WITH CHECK ({CLIENT} AND portal_visible);"
    for table in TABLES
)
OLD_POLICIES = "\n".join(
    f"ALTER POLICY {table}_portal_client_isolation ON {table} "
    f"USING ({CLIENT}) WITH CHECK ({CLIENT});"
    for table in TABLES
)
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
