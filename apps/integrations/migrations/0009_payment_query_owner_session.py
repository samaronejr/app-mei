"""AXRM-015: a stalled settlement owner keeps its consultation (A7.4).

`integrations.0008` presumed a runner dead as soon as its lease ran out, so a
live runner stalled past its deadline lost the page it had paid for. A runner
now also holds a session-level advisory lock keyed by its token for as long as
it runs (`payments._alive`); Postgres releases it only when that connection
ends, which a stall never does. `integrations_payment_runner_alive(owner)` reads
it from `pg_locks`, and the guard keeps refusing other writers while the owner's
lease is live OR its session lives. `payments._free` applies the same rule to a
takeover.

The reverse restores 0008's guard body and drops the function; neither holds
data, so it is clean on a populated database.
"""

from django.db import migrations

ALIVE = "integrations_payment_runner_alive"
GUARD = "integrations_paymentquery_owner_guard"

# A bigint advisory key shows its high half in `classid` and its low half in
# `objid`, with `objsubid` 1; `hashtextextended` is the key `_alive` locks.
FUNCTION = """
CREATE FUNCTION integrations_payment_runner_alive(owner uuid) RETURNS boolean
LANGUAGE sql VOLATILE AS $$
    SELECT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_locks AS held
        WHERE held.locktype = 'advisory'
            AND held.granted
            AND held.objsubid = 1
            AND held.database = (
                SELECT oid FROM pg_catalog.pg_database
                WHERE datname = current_database()
            )
            AND held.classid
                = ((hashtextextended(owner::text, 0) >> 32) & 4294967295)::oid
            AND held.objid = (hashtextextended(owner::text, 0) & 4294967295)::oid
    )
$$;
"""


def _guard(live: str) -> str:
    return f"""
CREATE OR REPLACE FUNCTION {GUARD}() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state IN ('na_fila', 'em_andamento')
        AND OLD.settlement_owner IS NOT NULL
        AND {live}
        AND OLD.settlement_owner::text IS DISTINCT FROM
            current_setting('app.payment_settlement_owner', true)
    THEN
        RAISE EXCEPTION
            'payment query % is settled by a live runner', OLD.id
            USING ERRCODE = 'object_in_use';
    END IF;
    RETURN NEW;
END;
$$;
"""


FORWARD = FUNCTION + _guard(
    f"""(
            OLD.settlement_until > statement_timestamp()
            OR {ALIVE}(OLD.settlement_owner)
        )"""
)
REVERSE = (
    _guard("OLD.settlement_until > statement_timestamp()")
    + f"DROP FUNCTION {ALIVE}(uuid);\n"
)


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0008_payment_query_settlement_owner"),
    ]

    operations = [migrations.RunSQL(FORWARD, REVERSE)]
