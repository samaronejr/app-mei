"""T13: `obligations_document.client_upload`, the fail-closed vault serve rule.

The portal serves a client-uploaded document only through a verified intake
(`apps.portal.views._served`). Which rows are client uploads is decided here for the
existing rows -- the uploader holds a client membership of the document's client --
and by the database default (TRUE) for every row written after: a writer that says
nothing gets the closed answer. A BEFORE INSERT trigger pins the portal role to TRUE,
so no portal write can present itself as a firm upload. (A trigger, not a second
portal policy: every portal policy is RESTRICTIVE FOR ALL with the client predicate,
tests/isolation/test_portal_rls_coverage.py.)

Reversible on a populated table: the trigger and its function are dropped, then the
column.
"""

from django.db import migrations, models

BACKFILL = """
UPDATE obligations_document AS document
SET client_upload = EXISTS (
    SELECT 1 FROM tenants_membership AS membership
    WHERE membership.user_id = document.uploaded_by_id
      AND membership.tenant_id = document.tenant_id
      AND membership.client_id = document.client_id
);
"""
TRIGGER = """
CREATE FUNCTION document_portal_client_upload() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF current_user = 'app_portal' AND NOT NEW.client_upload THEN
        RAISE EXCEPTION 'document_portal_client_upload: portal writes client uploads only'
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER document_portal_client_upload
    BEFORE INSERT ON obligations_document
    FOR EACH ROW EXECUTE FUNCTION document_portal_client_upload();
"""
DROP_TRIGGER = """
DROP TRIGGER IF EXISTS document_portal_client_upload ON obligations_document;
DROP FUNCTION IF EXISTS document_portal_client_upload();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("obligations", "0018_sourceobservation"),
        ("tenants", "0009_branding_and_superseded_tokens"),
    ]

    operations = [
        migrations.AddField(
            model_name="document",
            name="client_upload",
            field=models.BooleanField(
                db_default=True, default=True, verbose_name="uploaded by the client"
            ),
        ),
        migrations.RunSQL(BACKFILL, migrations.RunSQL.noop),
        migrations.RunSQL(TRIGGER, DROP_TRIGGER),
    ]
