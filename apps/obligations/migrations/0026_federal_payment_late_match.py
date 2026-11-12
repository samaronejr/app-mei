"""AXRM-015: a payment recorded before its DAS obligation existed is bound once.

`obligations_federalpayment` stays append-only for every role, owners included:
DELETE and any change to a reported field are refused. The single admitted UPDATE
sets `matched_obligation_id` from NULL to an obligation with every other column
unchanged, so an association can be made late but never re-pointed or removed. The
composite FK `federalpayment_obligation_client_fk` keeps it within the client.
`app_runtime` gets UPDATE on that one column only (no table-level UPDATE).

Reverse restores the strict trigger and revokes the column grant; existing rows
stay valid under it, so the reverse is clean on a populated database.
"""

from django.db import migrations

TABLE = "obligations_federalpayment"
TRIGGER = f"{TABLE}_append_only"
GUARD = "obligations_federalpayment_guard"

FORWARD = f"""
CREATE FUNCTION {GUARD}() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE'
        AND OLD.matched_obligation_id IS NULL
        AND NEW.matched_obligation_id IS NOT NULL
        AND to_jsonb(NEW) - 'matched_obligation_id'
            = to_jsonb(OLD) - 'matched_obligation_id'
    THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION
        'append-only table: % on %.% is not permitted',
        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

DROP TRIGGER {TRIGGER} ON {TABLE};
CREATE TRIGGER {TRIGGER}
    BEFORE UPDATE OR DELETE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION {GUARD}();

GRANT UPDATE (matched_obligation_id) ON {TABLE} TO app_runtime;
"""

REVERSE = f"""
REVOKE UPDATE (matched_obligation_id) ON {TABLE} FROM app_runtime;

DROP TRIGGER {TRIGGER} ON {TABLE};
CREATE TRIGGER {TRIGGER}
    BEFORE UPDATE OR DELETE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION audit_reject_mutation();

DROP FUNCTION {GUARD}();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("obligations", "0025_federal_record_guards"),
    ]

    operations = [
        migrations.RunSQL(FORWARD, REVERSE),
    ]
