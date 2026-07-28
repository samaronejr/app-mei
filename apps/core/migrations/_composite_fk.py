"""The tenant-crossing foreign key, expressed so PostgreSQL can enforce it.

PostgreSQL documents that referential-integrity checks **bypass row security**, in
order to keep the constraint meaningful for rows the current role cannot see. The
consequence for a multi-tenant schema is that a plain `child.parent_id` foreign key
proves only that the parent exists *somewhere in the table*. A `WITH CHECK` policy
asserting `tenant_id = current_setting(...)` inspects the child row and nothing else,
so it happily accepts a child whose parent belongs to another firm.

That is worse than an invisible row. The insert succeeds or fails depending on whether
the guessed parent id is real, which turns the constraint into an existence oracle for
another tenant's primary keys.

Pairing the tenant column into the key removes the possibility rather than detecting
it: `(tenant_id, parent_id)` must match a `(tenant_id, id)` pair that exists together,
so a cross-tenant reference cannot be written at all.

Named with a leading underscore because Django's migration loader imports every module
in a `migrations` package whose name does not start with `_` and raises
`BadMigrationError` when it finds no `Migration` class.
"""

from django.db import migrations

POSTGRES_IDENTIFIER_LIMIT = 63


def add_tenant_composite_fk(
    *,
    table: str,
    column: str,
    parent_table: str,
    constraint: str,
    tenant_column: str = "tenant_id",
) -> migrations.RunSQL:
    """Return the operation pair that adds and drops one composite tenant FK.

    `ON DELETE CASCADE` matches what the Django-level foreign key already promises
    and closes the same hole from the other side: a raw `DELETE` of a parent row
    cannot leave a child pointing at nothing.
    """
    if len(constraint) > POSTGRES_IDENTIFIER_LIMIT:
        msg = (
            f"constraint name {constraint!r} exceeds PostgreSQL's "
            f"{POSTGRES_IDENTIFIER_LIMIT}-character limit and would be silently "
            f"truncated, which can collide with another constraint"
        )
        raise ValueError(msg)
    return migrations.RunSQL(
        sql=(
            f'ALTER TABLE "{table}" ADD CONSTRAINT "{constraint}" '
            f'FOREIGN KEY ("{tenant_column}", "{column}") '
            f'REFERENCES "{parent_table}" ("{tenant_column}", "id") '
            f"ON DELETE CASCADE"
        ),
        reverse_sql=(f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{constraint}"'),
    )


__all__ = ["add_tenant_composite_fk"]
