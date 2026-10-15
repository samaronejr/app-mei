"""Extend T53's immutable binding guard to the exact import invitation origin."""

from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION delivery_notice_binding_guard() RETURNS trigger
LANGUAGE plpgsql AS $notice$
DECLARE
    binding jsonb;
    parent delivery_outboundmessage%ROWTYPE;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF ROW(NEW.notice_authorization_bytes, NEW.notice_authorization_digest)
           IS DISTINCT FROM
           ROW(OLD.notice_authorization_bytes, OLD.notice_authorization_digest)
           OR (OLD.notice_authorization_bytes IS NOT NULL AND
               ROW(NEW.id, NEW.tenant_id, NEW.client_id, NEW.kind, NEW.contact_id,
                   NEW.cycle_id, NEW.evidence_request_id, NEW.invite_id,
                   NEW.created_by_id, NEW.business_intent_key, NEW.retry_of_id,
                   NEW.content_digest)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.tenant_id, OLD.client_id, OLD.kind, OLD.contact_id,
                   OLD.cycle_id, OLD.evidence_request_id, OLD.invite_id,
                   OLD.created_by_id, OLD.business_intent_key, OLD.retry_of_id,
                   OLD.content_digest)) THEN
            RAISE EXCEPTION 'notice_authority_immutable' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.kind IN ('approved_email', 'approved_publication_notice') THEN
        IF NEW.notice_authorization_bytes IS NOT NULL
           OR NEW.notice_authorization_digest IS NOT NULL THEN
            RAISE EXCEPTION 'consequential_notice_authority' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.notice_authorization_bytes IS NULL
       OR NEW.notice_authorization_digest IS NULL
       OR sha256(NEW.notice_authorization_bytes)
          IS DISTINCT FROM NEW.notice_authorization_digest THEN
        RAISE EXCEPTION 'notice_authority_required' USING ERRCODE = '23514';
    END IF;
    binding := convert_from(NEW.notice_authorization_bytes, 'UTF8')::jsonb;
    IF binding->>'schema' IS DISTINCT FROM 'axorim.notice-authorization.v1'
       OR binding->>'mode' IS DISTINCT FROM 'manual'
       OR binding->>'feature' IS DISTINCT FROM
          (CASE WHEN NEW.kind = 'contact_portal_invite' THEN 'import_r1'
               ELSE 'cycles_r1' END)
       OR binding->>'kind' IS DISTINCT FROM NEW.kind
       OR binding->>'tenant_id' IS DISTINCT FROM NEW.tenant_id::text
       OR binding->>'client_id' IS DISTINCT FROM NEW.client_id::text
       OR binding->>'contact_id' IS DISTINCT FROM NEW.contact_id::text
       OR binding->>'cycle_id' IS DISTINCT FROM NEW.cycle_id::text
       OR binding->>'request_id' IS DISTINCT FROM NEW.evidence_request_id::text
       OR binding->>'invite_id' IS DISTINCT FROM NEW.invite_id::text
       OR binding->>'issuer_id' IS DISTINCT FROM NEW.created_by_id::text
       OR binding->>'content_digest' IS DISTINCT FROM NEW.content_digest
       OR NEW.kind NOT IN
          ('notice', 'portal_invite', 'contact_portal_invite',
           'request_notice', 'request_reminder')
       OR coalesce(binding->'basis'->>'type', '') NOT IN ('playbook', 'tenant_policy') THEN
        RAISE EXCEPTION 'notice_authority_identity' USING ERRCODE = '23514';
    END IF;
    IF NEW.retry_of_id IS NULL THEN
        IF binding->>'root_intent' IS DISTINCT FROM NEW.business_intent_key THEN
            RAISE EXCEPTION 'notice_authority_root' USING ERRCODE = '23514';
        END IF;
    ELSE
        SELECT * INTO parent FROM delivery_outboundmessage
         WHERE id = NEW.retry_of_id AND tenant_id = NEW.tenant_id
           AND client_id = NEW.client_id;
        IF NOT FOUND
           OR NEW.notice_authorization_bytes IS DISTINCT FROM parent.notice_authorization_bytes
           OR NEW.notice_authorization_digest IS DISTINCT FROM parent.notice_authorization_digest
           OR NEW.kind IS DISTINCT FROM parent.kind
           OR NEW.content_digest IS DISTINCT FROM parent.content_digest
           OR NEW.created_by_id IS DISTINCT FROM parent.created_by_id
           OR NEW.contact_id IS DISTINCT FROM parent.contact_id
           OR NEW.cycle_id IS DISTINCT FROM parent.cycle_id
           OR NEW.evidence_request_id IS DISTINCT FROM parent.evidence_request_id
           OR NEW.invite_id IS DISTINCT FROM parent.invite_id THEN
            RAISE EXCEPTION 'notice_authority_retry_lineage' USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$notice$;
"""

# Reverse only the two origin extensions; all T53 refusal and lineage checks remain.
REVERSE = FORWARD.replace(
    """(CASE WHEN NEW.kind = 'contact_portal_invite' THEN 'import_r1'
               ELSE 'cycles_r1' END)""",
    "'cycles_r1'",
).replace(
    """('notice', 'portal_invite', 'contact_portal_invite',
           'request_notice', 'request_reminder')""",
    "('notice', 'portal_invite', 'request_notice', 'request_reminder')",
)


class Migration(migrations.Migration):
    dependencies = [("delivery", "0006_notice_authorization")]

    operations = [migrations.RunSQL(FORWARD, REVERSE)]
