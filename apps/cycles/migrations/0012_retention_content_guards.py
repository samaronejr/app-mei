"""Permit only eligible, claimed content erasure through otherwise frozen records."""

from importlib import import_module

from django.db import migrations

PAYLOAD_SQL = """
CREATE FUNCTION cycles_redact_payload(data jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE k text; result jsonb := data;
BEGIN
    FOREACH k IN ARRAY ARRAY['subject','body','reason'] LOOP
        IF result ? k THEN result := jsonb_set(result, ARRAY[k], '"[retention-redacted]"'); END IF;
    END LOOP;
    IF result ? 'mime_base64' THEN result := jsonb_set(result, '{mime_base64}', '""'); END IF;
    IF result ? 'headers' THEN result := jsonb_set(result, '{headers}', '{}'); END IF;
    IF jsonb_typeof(result->'notice')='object' THEN
        result := jsonb_set(result, '{notice}', cycles_redact_payload(result->'notice'));
    END IF;
    IF jsonb_typeof(result->'completion_reasons')='object' THEN
        result := jsonb_set(result, '{completion_reasons}', COALESCE(
            (SELECT jsonb_object_agg(key, '[retention-redacted]'::text)
               FROM jsonb_each(result->'completion_reasons')), '{}'::jsonb));
    END IF;
    RETURN result;
END; $$;
"""

PROPOSAL_GUARD = """
    IF TG_OP='UPDATE' AND OLD.redacted_at IS NOT NULL THEN
        IF (to_jsonb(NEW)-ARRAY['state','version','invalidation_reasons','approved_at'])
            IS DISTINCT FROM
           (to_jsonb(OLD)-ARRAY['state','version','invalidation_reasons','approved_at']) THEN
            RAISE EXCEPTION 'redacted proof cannot be rewritten' USING ERRCODE='insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP='UPDATE' AND NEW.redacted_at IS NOT NULL AND OLD.redacted_at IS NULL THEN
        IF OLD.created_at + (CASE WHEN OLD.action_kind IN ('email_notice','client_publication')
            THEN interval '2 years' ELSE interval '5 years' END) > clock_timestamp()
            OR NOT EXISTS (SELECT 1 FROM cycles_contentpurgeclaim c
                WHERE c.tenant_id=OLD.tenant_id AND c.client_id=OLD.client_id
                  AND c.content_kind='proposal' AND c.content_id=OLD.id AND c.completed_at IS NULL)
            OR EXISTS (SELECT 1 FROM cycles_legalhold h WHERE h.tenant_id=OLD.tenant_id
                AND h.client_id=OLD.client_id AND h.released_at IS NULL)
            OR (to_jsonb(NEW)-ARRAY['manifest_bytes','input_spec','decision_reason','redacted_at'])
                IS DISTINCT FROM
               (to_jsonb(OLD)-ARRAY['manifest_bytes','input_spec','decision_reason','redacted_at'])
            OR NEW.decision_reason <> ''
            OR NEW.input_spec IS DISTINCT FROM cycles_redact_payload(OLD.input_spec)
            OR convert_from(NEW.manifest_bytes,'UTF8')::jsonb IS DISTINCT FROM
               jsonb_set(convert_from(OLD.manifest_bytes,'UTF8')::jsonb, '{payload}',
                  cycles_redact_payload(convert_from(OLD.manifest_bytes,'UTF8')::jsonb->'payload'))
        THEN
            RAISE EXCEPTION 'proposal content is retained' USING ERRCODE='insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
"""
PUBLICATION_GUARD = """
    IF TG_OP='UPDATE' AND NEW.redacted_at IS NOT NULL AND OLD.redacted_at IS NULL THEN
        IF OLD.published_at + interval '2 years' > clock_timestamp()
            OR NOT EXISTS (SELECT 1 FROM cycles_contentpurgeclaim c
                WHERE c.tenant_id=OLD.tenant_id AND c.client_id=OLD.client_id
                  AND c.content_kind='publication' AND c.content_id=OLD.id AND c.completed_at IS NULL)
            OR EXISTS (SELECT 1 FROM cycles_legalhold h WHERE h.tenant_id=OLD.tenant_id
                AND h.client_id=OLD.client_id AND h.released_at IS NULL)
            OR (to_jsonb(NEW)-ARRAY['title','body_ptbr','redacted_at']) IS DISTINCT FROM
               (to_jsonb(OLD)-ARRAY['title','body_ptbr','redacted_at'])
            OR NEW.title <> '[retention-redacted]' OR NEW.body_ptbr <> '[retention-redacted]'
        THEN
            RAISE EXCEPTION 'publication content is retained' USING ERRCODE='insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;
"""


def function_sql(module: str, attribute: str, name: str) -> str:
    """Reuse the frozen preceding migration, not current application code."""
    source: str = getattr(import_module(module), attribute)
    begin = source.index(f"CREATE FUNCTION {name}()")
    end = source.index(f"CREATE TRIGGER {name}", begin)
    return source[begin:end].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


PROPOSAL = function_sql(
    "apps.cycles.migrations.0008_exact_effect_approvals",
    "PROTECTION_SQL",
    "cycles_frozen_proposal",
)
PUBLICATION = function_sql(
    "apps.cycles.migrations.0009_client_publications",
    "EXACT_SQL",
    "cycles_frozen_publication",
)


class Migration(migrations.Migration):
    dependencies = [("cycles", "0011_actionproposal_redacted_at_and_more")]
    operations = [
        migrations.RunSQL(PAYLOAD_SQL, "DROP FUNCTION cycles_redact_payload(jsonb);"),
        migrations.RunSQL(
            PROPOSAL.replace("BEGIN\n", "BEGIN\n" + PROPOSAL_GUARD, 1), PROPOSAL
        ),
        migrations.RunSQL(
            PUBLICATION.replace("BEGIN\n", "BEGIN\n" + PUBLICATION_GUARD, 1),
            PUBLICATION,
        ),
    ]
