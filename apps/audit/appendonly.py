"""The migration operations that make an audit table genuinely append-only.

Two independent controls, because either one alone is defeated by a role this project
actually uses:

* **Grants** stop `app_runtime` — the role the web process connects as. They do not
  stop `app_migrator` or `app_test`, which OWN these tables and hold `BYPASSRLS`.
* **A trigger** stops everyone, owners included. PostgreSQL runs `BEFORE UPDATE OR
  DELETE` triggers regardless of who issued the statement, so this is the control that
  survives a migration, a fixture, or an operator with the migration credentials.

Grants are enumerated rather than blanket. `GRANT ALL` would silently hand back
`TRUNCATE`, which is a privilege of its own, is documented as **not** subject to
row-level security, and would empty the table without firing a single row trigger —
breaking append-only while every test still passed.
"""

from django.db import migrations

REJECT_FUNCTION = "audit_reject_mutation"

CREATE_REJECT_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {REJECT_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'append-only table: % on %.% is not permitted',
        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END;
$$;
"""

DROP_REJECT_FUNCTION = f"DROP FUNCTION IF EXISTS {REJECT_FUNCTION}()"


def append_only_sql(table: str, role: str = "app_runtime") -> str:
    """Return the statements that make one table append-only for every role."""
    return f"""
CREATE TRIGGER {table}_append_only
    BEFORE UPDATE OR DELETE ON {table}
    FOR EACH ROW EXECUTE FUNCTION {REJECT_FUNCTION}();

REVOKE ALL ON {table} FROM {role};
GRANT SELECT, INSERT ON {table} TO {role};
"""


def append_only_reverse_sql(table: str, role: str = "app_runtime") -> str:
    """Return the statements that undo `append_only_sql` for one table."""
    return f"""
DROP TRIGGER IF EXISTS {table}_append_only ON {table};
GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role};
"""


def make_append_only(*tables: str) -> list[migrations.RunSQL]:
    """Build the operations that lock a set of audit tables against rewriting."""
    operations = [
        migrations.RunSQL(
            sql=CREATE_REJECT_FUNCTION,
            reverse_sql=DROP_REJECT_FUNCTION,
        ),
    ]
    operations.extend(
        migrations.RunSQL(
            sql=append_only_sql(table),
            reverse_sql=append_only_reverse_sql(table),
        )
        for table in tables
    )
    return operations
