"""Assign fiscal audit order at insert time, including old-code writes."""

from django.db import migrations, models

_INSTALL = """
CREATE SEQUENCE public.audit_fiscal_event_order AS bigint CACHE 1 NO CYCLE
    OWNED BY public.audit_event.fiscal_order;
GRANT USAGE ON SEQUENCE public.audit_fiscal_event_order TO app_runtime;

CREATE FUNCTION public.audit_assign_fiscal_event_order() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    NEW.fiscal_order := nextval('public.audit_fiscal_event_order'::regclass);
    RETURN NEW;
END;
$$;

CREATE TRIGGER audit_event_fiscal_order
    BEFORE INSERT ON public.audit_event
    FOR EACH ROW WHEN (NEW.action IN (
        'fiscal.revision_registered', 'fiscal.head_changed',
        'fiscal.consistency_incident', 'fiscal.iss_evidence_recovered'
    ))
    EXECUTE FUNCTION public.audit_assign_fiscal_event_order();
"""

_UNINSTALL = """
DROP TRIGGER audit_event_fiscal_order ON public.audit_event;
DROP FUNCTION public.audit_assign_fiscal_event_order();
DROP SEQUENCE public.audit_fiscal_event_order;
"""


class Migration(migrations.Migration):
    dependencies = [("audit", "0007_datasubjectrequest_notification_bookkeeping")]

    operations = [
        migrations.AddField(
            model_name="event",
            name="fiscal_order",
            field=models.BigIntegerField(null=True, editable=False),
        ),
        migrations.RunSQL(_INSTALL, _UNINSTALL),
    ]
