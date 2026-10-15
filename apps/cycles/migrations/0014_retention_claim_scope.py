"""Constrain purge identities and serialize new links with content deletion."""

from django.db import migrations

SQL = """
CREATE FUNCTION cycles_purge_identity() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_table text;
    found_target boolean;
    expected_keys jsonb := '[]'::jsonb;
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'purge proof cannot be deleted' USING ERRCODE='insufficient_privilege';
    END IF;
    IF TG_OP='UPDATE' THEN
        IF (to_jsonb(NEW)-ARRAY['completed_at','error_code'])
            IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['completed_at','error_code'])
            OR (OLD.completed_at IS NOT NULL AND NEW IS DISTINCT FROM OLD) THEN
            RAISE EXCEPTION 'purge identity is frozen' USING ERRCODE='insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
    target_table := CASE NEW.content_kind
        WHEN 'document' THEN 'obligations_document'
        WHEN 'export' THEN 'cycles_evidenceexport'
        WHEN 'message' THEN 'delivery_outboundmessage'
        WHEN 'proposal' THEN 'cycles_actionproposal'
        WHEN 'publication' THEN 'cycles_clientpublication'
        WHEN 'ai_derived' THEN 'cycles_extractedfield'
        WHEN 'note' THEN 'cycles_internalnote'
        WHEN 'amendment' THEN 'cycles_cycleamendment'
        WHEN 'justification' THEN 'cycles_completionjustification'
        WHEN 'request_internal' THEN 'cycles_requestinternal'
        WHEN 'exception_internal' THEN 'cycles_exceptioninternal'
        ELSE NULL END;
    IF target_table IS NULL THEN
        RAISE EXCEPTION 'unknown purge kind' USING ERRCODE='check_violation';
    END IF;
    EXECUTE format('SELECT EXISTS(SELECT 1 FROM public.%I
        WHERE tenant_id=$1 AND client_id=$2 AND id=$3)', target_table)
        INTO found_target USING NEW.tenant_id, NEW.client_id, NEW.content_id;
    IF NOT found_target THEN
        RAISE EXCEPTION 'purge content outside client' USING ERRCODE='check_violation';
    END IF;
    IF NEW.content_kind='document' THEN
        SELECT jsonb_agg(key ORDER BY key) INTO expected_keys FROM (
            SELECT storage_key AS key FROM obligations_document
             WHERE tenant_id=NEW.tenant_id AND client_id=NEW.client_id AND id=NEW.content_id
            UNION
            SELECT quarantine_key FROM cycles_documentintake
             WHERE tenant_id=NEW.tenant_id AND client_id=NEW.client_id
               AND document_id=NEW.content_id AND quarantine_key <> ''
        ) keys;
    ELSIF NEW.content_kind='export' THEN
        SELECT jsonb_build_array(storage_key) INTO expected_keys FROM cycles_evidenceexport
         WHERE tenant_id=NEW.tenant_id AND client_id=NEW.client_id AND id=NEW.content_id
           AND state='ready' AND storage_key <> '';
    END IF;
    IF NEW.storage_keys IS DISTINCT FROM expected_keys OR NEW.completed_at IS NOT NULL THEN
        RAISE EXCEPTION 'purge storage descriptor differs' USING ERRCODE='check_violation';
    END IF;
    RETURN NEW;
END; $$;
CREATE TRIGGER contentpurge_identity BEFORE INSERT OR UPDATE OR DELETE
ON cycles_contentpurgeclaim FOR EACH ROW EXECUTE FUNCTION cycles_purge_identity();

-- Portal cannot SELECT private claims or UPDATE a client row. This trigger alone
-- borrows its owner's rights, with explicit ambient tenant/client checks, fully
-- qualified tables and an empty search_path. It exposes no data or callable API.
CREATE FUNCTION cycles_link_retention() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    IF NEW.tenant_id::text IS DISTINCT FROM current_setting('app.tenant_id', true)
        OR (NULLIF(current_setting('app.client_id', true),'') IS NOT NULL
            AND NEW.client_id::text IS DISTINCT FROM current_setting('app.client_id', true))
    THEN
        RAISE EXCEPTION 'link outside retention scope' USING ERRCODE='check_violation',
            CONSTRAINT='evidencelink_retention_scope';
    END IF;
    PERFORM id FROM public.clients_clientcompany
     WHERE tenant_id=NEW.tenant_id AND id=NEW.client_id FOR NO KEY UPDATE;
    IF EXISTS (SELECT 1 FROM public.obligations_document
        WHERE tenant_id=NEW.tenant_id AND client_id=NEW.client_id
          AND id=NEW.document_id AND purged_at IS NOT NULL)
        OR EXISTS (SELECT 1 FROM public.cycles_contentpurgeclaim
        WHERE tenant_id=NEW.tenant_id AND client_id=NEW.client_id
          AND content_kind='document' AND content_id=NEW.document_id)
    THEN
        RAISE EXCEPTION 'original content is no longer available' USING ERRCODE='check_violation',
            CONSTRAINT='evidencelink_retained_original';
    END IF;
    RETURN NEW;
END; $$;
REVOKE ALL ON FUNCTION cycles_link_retention() FROM PUBLIC;
-- Native RLS WITH CHECK must reject forged clients before privileged trigger work.
-- The client fence still lasts until commit; a claimed original rolls back this insert.
CREATE TRIGGER evidencelink_retention AFTER INSERT ON cycles_evidencelink
FOR EACH ROW EXECUTE FUNCTION cycles_link_retention();

CREATE FUNCTION cycles_frozen_justification() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'completion justification cannot be deleted'
            USING ERRCODE='insufficient_privilege';
    END IF;
    IF (to_jsonb(NEW)-ARRAY['reasons','redacted_at'])
        IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['reasons','redacted_at'])
        OR OLD.redacted_at IS NOT NULL OR NEW.redacted_at IS NULL OR NEW.reasons <> '{}'::jsonb
        OR NOT EXISTS (SELECT 1 FROM cycles_completiongeneration g
            WHERE g.tenant_id=OLD.tenant_id AND g.client_id=OLD.client_id
              AND g.id=OLD.generation_id AND g.completed_at + interval '5 years' <= clock_timestamp())
        OR NOT EXISTS (SELECT 1 FROM cycles_contentpurgeclaim c
            WHERE c.tenant_id=OLD.tenant_id AND c.client_id=OLD.client_id
              AND c.content_kind='justification' AND c.content_id=OLD.id AND c.completed_at IS NULL)
        OR EXISTS (SELECT 1 FROM cycles_legalhold h WHERE h.tenant_id=OLD.tenant_id
            AND h.client_id=OLD.client_id AND h.released_at IS NULL)
    THEN
        RAISE EXCEPTION 'completion justification is frozen'
            USING ERRCODE='insufficient_privilege';
    END IF;
    RETURN NEW;
END; $$;
CREATE TRIGGER completionjustification_frozen BEFORE UPDATE OR DELETE
ON cycles_completionjustification FOR EACH ROW EXECUTE FUNCTION cycles_frozen_justification();
"""

REVERSE_SQL = """
DROP TRIGGER completionjustification_frozen ON cycles_completionjustification;
DROP FUNCTION cycles_frozen_justification();
DROP TRIGGER evidencelink_retention ON cycles_evidencelink;
DROP FUNCTION cycles_link_retention();
DROP TRIGGER contentpurge_identity ON cycles_contentpurgeclaim;
DROP FUNCTION cycles_purge_identity();
"""


class Migration(migrations.Migration):
    """Every reverse removes only this migration's own functions and triggers."""

    dependencies = [("cycles", "0013_publication_supersession")]
    operations = [migrations.RunSQL(SQL, REVERSE_SQL)]
