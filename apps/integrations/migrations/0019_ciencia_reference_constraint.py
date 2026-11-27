"""Keep exact-reference integrity without masking a tenant/client FK refusal."""

from django.db import migrations

FORWARD = """
CREATE FUNCTION enforce_providercall_ciencia_exact_refs()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT (
        (NEW.proposal_id IS NULL AND NEW.fiscal_revision_id IS NULL)
        OR (
            NEW.operation IN ('FC-CIE', 'FC-CIE-R')
            AND NEW.proposal_id IS NOT NULL
            AND NEW.fiscal_revision_id IS NOT NULL
        )
    ) THEN
        RAISE EXCEPTION
            'provider call violates constraint "providercall_ciencia_exact_refs"'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'providercall_ciencia_exact_refs';
    END IF;
    RETURN NEW;
END;
$$;

-- Native RI_ConstraintTrigger_* checks sort before this AFTER ROW trigger.
-- NOT DEFERRABLE preserves immediate pair/operation integrity even when a
-- caller issues SET CONSTRAINTS ALL DEFERRED.
CREATE CONSTRAINT TRIGGER providercall_ciencia_exact_refs
AFTER INSERT OR UPDATE ON integrations_providercall
NOT DEFERRABLE
FOR EACH ROW EXECUTE FUNCTION enforce_providercall_ciencia_exact_refs();
"""

REVERSE = """
DROP TRIGGER providercall_ciencia_exact_refs ON integrations_providercall;
DROP FUNCTION enforce_providercall_ciencia_exact_refs();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0018_ciencia_professional_note"),
    ]

    operations = [
        # Atomic DDL holds the table lock until the equivalent trigger is installed.
        migrations.RemoveConstraint(
            model_name="providercall",
            name="providercall_ciencia_exact_refs",
        ),
        migrations.RunSQL(FORWARD, REVERSE),
    ]
