"""AXRM-015: one live runner settles a PAGTOWEB consultation (A7.4).

A runner takes `settlement_owner` (its token) with `settlement_until` (database
time) before it pays for a page, and renews both on every later page. While that
lease is live and the consultation is active, the trigger refuses every UPDATE
from a transaction that has not declared itself the owner through the
transaction-local `app.payment_settlement_owner` setting. Another runner therefore
cannot finish, advance or take over the consultation; once the lease has run out
the owner is presumed dead and a redelivered runner may resume it.

Both columns are nullable and the reverse drops the trigger and the columns, so it
is clean on a populated database (only transient lease markers are lost).
"""

from django.db import migrations, models

TABLE = "integrations_paymentquery"
GUARD = "integrations_paymentquery_owner_guard"

FORWARD = f"""
CREATE FUNCTION {GUARD}() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state IN ('na_fila', 'em_andamento')
        AND OLD.settlement_owner IS NOT NULL
        AND OLD.settlement_until > statement_timestamp()
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

CREATE TRIGGER {GUARD}
    BEFORE UPDATE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION {GUARD}();
"""

REVERSE = f"""
DROP TRIGGER {GUARD} ON {TABLE};
DROP FUNCTION {GUARD}();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0007_payment_queries"),
    ]

    operations = [
        migrations.AddField(
            model_name="paymentquery",
            name="settlement_owner",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="paymentquery",
            name="settlement_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="paymentquery",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("settlement_owner__isnull", True),
                    ("settlement_until__isnull", True),
                )
                | models.Q(
                    ("settlement_owner__isnull", False),
                    ("settlement_until__isnull", False),
                ),
                name="paymentquery_owner_lease",
            ),
        ),
        migrations.RunSQL(FORWARD, REVERSE),
    ]
