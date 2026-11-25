"""AXRM-049-PORTAL: hide internal requests without hiding approved artifacts."""

# CROSS_TENANT_OK: Repair each tenant's firm-only request history before the
# shared portal policies take effect; rollback checks every tenant for protected history.

from django.db import migrations

from apps.cycles.migrations._request_cutover import (
    install,
    refuse_exposure,
    remove_lifetime,
)

# The migrator (app_test in tests) owns this function and bypasses forced RLS.
# Its only entry key is a published receipt and its selected link, never a
# request key. Recheck both ambient scope dimensions and the live join that
# publication_download used before links to internal requests became invisible.
# The scalar result is an already published document id, never a request field.
# Pin catalog resolution even with a caller's temporary schema present.
LOOKUP = """
CREATE FUNCTION public.cycles_portal_publication_document(
    publication_id pg_catalog.uuid, link_id pg_catalog.uuid
)
RETURNS pg_catalog.uuid
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $$
    SELECT d.id
    FROM public.cycles_clientpublication p
    JOIN public.cycles_evidencelink l
      ON l.id OPERATOR(pg_catalog.=) $2
     AND (l.tenant_id, l.client_id) OPERATOR(pg_catalog.=) (p.tenant_id, p.client_id)
    JOIN public.cycles_evidencerequest r
      ON r.id OPERATOR(pg_catalog.=) l.request_id
     AND (r.tenant_id, r.client_id) OPERATOR(pg_catalog.=) (p.tenant_id, p.client_id)
     AND r.cycle_id OPERATOR(pg_catalog.=) p.cycle_id
    JOIN public.obligations_document d
      ON d.id OPERATOR(pg_catalog.=) l.document_id
     AND (d.tenant_id, d.client_id) OPERATOR(pg_catalog.=) (p.tenant_id, p.client_id)
    WHERE p.id OPERATOR(pg_catalog.=) $1
      AND p.state OPERATOR(pg_catalog.=) 'publicada'
      AND p.tenant_id::pg_catalog.text OPERATOR(pg_catalog.=)
          pg_catalog.current_setting('app.tenant_id', true)
      AND p.client_id::pg_catalog.text OPERATOR(pg_catalog.=)
          pg_catalog.current_setting('app.client_id', true)
      AND l.review_decision OPERATOR(pg_catalog.=) 'aceito'
      AND d.portal_visible
      AND NOT EXISTS (
          SELECT 1 FROM public.obligations_obligation o
          WHERE (o.tenant_id, o.client_id) OPERATOR(pg_catalog.=) (p.tenant_id, p.client_id)
            AND (
                o.id OPERATOR(pg_catalog.=) r.obligation_id
                OR o.id OPERATOR(pg_catalog.=) d.obligation_id
            ) AND NOT o.portal_visible
      )
      AND EXISTS (
          SELECT 1 FROM pg_catalog.jsonb_array_elements(p.documents) AS m(artifact)
          WHERE (m.artifact OPERATOR(pg_catalog.->>) 'link_id')
                    OPERATOR(pg_catalog.=) l.id::pg_catalog.text
            AND (m.artifact OPERATOR(pg_catalog.->>) 'link_revision')
                    OPERATOR(pg_catalog.=) l.version::pg_catalog.text
            AND (m.artifact OPERATOR(pg_catalog.->>) 'document_id')
                    OPERATOR(pg_catalog.=) d.id::pg_catalog.text
            AND (m.artifact OPERATOR(pg_catalog.->>) 'sha256')
                    OPERATOR(pg_catalog.=) d.sha256
            AND (m.artifact OPERATOR(pg_catalog.->>) 'size')
                    OPERATOR(pg_catalog.=) d.byte_size::pg_catalog.text
      );
$$;
REVOKE ALL ON FUNCTION public.cycles_portal_publication_document(
    pg_catalog.uuid, pg_catalog.uuid
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.cycles_portal_publication_document(
    pg_catalog.uuid, pg_catalog.uuid
) TO app_portal;
"""
DROP_LOOKUP = """
DROP FUNCTION public.cycles_portal_publication_document(pg_catalog.uuid, pg_catalog.uuid);
"""


class Migration(migrations.Migration):
    dependencies = [
        ("cycles", "0024_fiscal_review_successor"),
        ("obligations", "0023_document_portal_visibility"),
    ]

    operations = [
        # First installation: no newly active portal policy can observe an
        # unreconciled formerly-internal row. The migration transaction holds
        # the request/link locks until both new policy clauses are in place.
        migrations.RunPython(install, remove_lifetime),
        migrations.RunSQL(LOOKUP, DROP_LOOKUP),
        migrations.RunPython(migrations.RunPython.noop, refuse_exposure),
    ]
