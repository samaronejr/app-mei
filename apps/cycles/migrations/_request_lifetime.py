"""Frozen row guards for the AXRM-049 portal cutover."""

LOCK_VISIBILITY = """
CREATE FUNCTION public.cycles_keep_internal_request()
RETURNS pg_catalog.trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    RAISE check_violation USING CONSTRAINT = 'evidencerequest_internal_lifetime',
        MESSAGE = 'Internal request classification and identity are fixed.';
END;
$$;
REVOKE ALL ON FUNCTION public.cycles_keep_internal_request() FROM PUBLIC;
-- Classification is assigned on INSERT. Review may save the same state.
CREATE TRIGGER cycles_keep_internal_request
AFTER UPDATE ON public.cycles_evidencerequest
FOR EACH ROW WHEN (
    (OLD.state OPERATOR(pg_catalog.=) 'interno' OR NEW.state OPERATOR(pg_catalog.=) 'interno')
    AND (NEW.state IS DISTINCT FROM OLD.state OR NEW.id IS DISTINCT FROM OLD.id)
)
EXECUTE FUNCTION public.cycles_keep_internal_request();
CREATE TRIGGER cycles_keep_internal_request_identity
BEFORE DELETE ON public.cycles_evidencerequest
FOR EACH ROW WHEN (OLD.state OPERATOR(pg_catalog.=) 'interno')
EXECUTE FUNCTION public.cycles_keep_internal_request();

CREATE FUNCTION public.cycles_keep_internal_link()
RETURNS pg_catalog.trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    -- Fail closed if the old parent cannot be read in the writer's tenant.
    IF NOT EXISTS (
        SELECT 1 FROM public.cycles_evidencerequest r
        WHERE r.id OPERATOR(pg_catalog.=) OLD.request_id
          AND r.tenant_id OPERATOR(pg_catalog.=) OLD.tenant_id
          AND r.client_id OPERATOR(pg_catalog.=) OLD.client_id
          AND r.state OPERATOR(pg_catalog.<>) 'interno'
    ) THEN
        RAISE check_violation USING CONSTRAINT = 'evidencelink_internal_lifetime',
            MESSAGE = 'A link to an internal request cannot change parent or identity.';
    END IF;
    RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION public.cycles_keep_internal_link() FROM PUBLIC;
CREATE TRIGGER cycles_keep_internal_link AFTER UPDATE ON public.cycles_evidencelink
FOR EACH ROW WHEN (
    OLD.request_id OPERATOR(pg_catalog.<>) NEW.request_id
    OR OLD.id OPERATOR(pg_catalog.<>) NEW.id
)
EXECUTE FUNCTION public.cycles_keep_internal_link();
"""

UNLOCK_VISIBILITY = """
DROP TRIGGER IF EXISTS cycles_keep_internal_link ON public.cycles_evidencelink;
DROP FUNCTION IF EXISTS public.cycles_keep_internal_link();
DROP TRIGGER IF EXISTS cycles_keep_internal_request_identity ON public.cycles_evidencerequest;
DROP TRIGGER IF EXISTS cycles_keep_internal_request ON public.cycles_evidencerequest;
DROP FUNCTION IF EXISTS public.cycles_keep_internal_request();
"""
