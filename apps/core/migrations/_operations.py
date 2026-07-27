"""The reusable migration operation that makes a table tenant-isolated.

Named with a leading underscore deliberately: Django's migration loader imports every
module in a `migrations` package whose name does not start with `_` or `~` and raises
`BadMigrationError` if it has no `Migration` class. A file called `operations.py` here
would break `migrate` on import.
"""

from typing import Any

from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ProjectState

POLICY_SUFFIX = "_tenant_isolation"
POSTGRES_IDENTIFIER_LIMIT = 63

# Three separate traps are encoded in this one predicate:
#
#   missing_ok (the `, true`) — without it, `current_setting` RAISES on a connection
#   that never set the GUC, which turns a fail-closed read into a 500.
#
#   NULLIF(..., '')          — a recycled connection, and every production login,
#   leaves the GUC as the empty string, and `''::uuid` raises SQLSTATE 22P02.
#
#   comparison to NULL       — `tenant_id = NULL` is NULL, never true, so no context
#   means no rows. That is the fail-closed default the whole design rests on.
TENANT_PREDICATE = "{column} = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


class EnableRLS(Operation):
    """Enable, force, and police row-level security on one tenant-scoped table.

    `ENABLE` alone is not enough: the table owner bypasses row-level security unless
    `FORCE` is also set, and migrations create these tables as their owner. `WITH
    CHECK` is not optional either — without it a tenant may INSERT or UPDATE a row
    *into* another tenant, which `USING` alone does not prevent.
    """

    reversible = True
    reduces_to_sql = True

    def __init__(self, model_name: str, tenant_field: str = "tenant_id") -> None:
        self.model_name = model_name
        self.tenant_field = tenant_field

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """Serialize the operation back into migration source."""
        kwargs: dict[str, Any] = {"model_name": self.model_name}
        if self.tenant_field != "tenant_id":
            kwargs["tenant_field"] = self.tenant_field
        return (self.__class__.__qualname__, [], kwargs)

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        """Change nothing: policies live in the database, not in migration state."""

    def _policy_name(self, table: str) -> str:
        name = f"{table}{POLICY_SUFFIX}"
        if len(name) <= POSTGRES_IDENTIFIER_LIMIT:
            return name
        # PostgreSQL silently truncates over-long identifiers, which would let two
        # tables collide on one policy name. Truncate the table part explicitly.
        keep = POSTGRES_IDENTIFIER_LIMIT - len(POLICY_SUFFIX)
        return f"{table[:keep]}{POLICY_SUFFIX}"

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        """Apply `ENABLE`, `FORCE`, and the fail-closed policy."""
        model = to_state.apps.get_model(app_label, self.model_name)
        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return
        table = model._meta.db_table
        quoted_table = schema_editor.quote_name(table)
        policy = schema_editor.quote_name(self._policy_name(table))
        predicate = TENANT_PREDICATE.format(
            column=schema_editor.quote_name(self.tenant_field),
        )
        schema_editor.execute(
            f"ALTER TABLE {quoted_table} ENABLE ROW LEVEL SECURITY",
            params=None,
        )
        schema_editor.execute(
            f"ALTER TABLE {quoted_table} FORCE ROW LEVEL SECURITY",
            params=None,
        )
        schema_editor.execute(
            f"CREATE POLICY {policy} ON {quoted_table} "
            f"FOR ALL USING ({predicate}) WITH CHECK ({predicate})",
            params=None,
        )

    def database_backwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        """Drop the policy and lift row-level security again."""
        model = from_state.apps.get_model(app_label, self.model_name)
        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return
        table = model._meta.db_table
        quoted_table = schema_editor.quote_name(table)
        policy = schema_editor.quote_name(self._policy_name(table))
        schema_editor.execute(
            f"DROP POLICY IF EXISTS {policy} ON {quoted_table}",
            params=None,
        )
        schema_editor.execute(
            f"ALTER TABLE {quoted_table} NO FORCE ROW LEVEL SECURITY",
            params=None,
        )
        schema_editor.execute(
            f"ALTER TABLE {quoted_table} DISABLE ROW LEVEL SECURITY",
            params=None,
        )

    def describe(self) -> str:
        """Describe the operation for `migrate --plan` and `sqlmigrate`."""
        return f"Enable forced row-level security on {self.model_name}"

    @property
    def migration_name_fragment(self) -> str:
        """Supply the auto-generated migration filename fragment."""
        return f"enable_rls_{self.model_name.lower()}"
