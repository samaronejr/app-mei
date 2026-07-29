"""The reusable migration operation that makes a table tenant-isolated.

Named with a leading underscore deliberately: Django's migration loader imports every
module in a `migrations` package whose name does not start with `_` or `~` and raises
`BadMigrationError` if it has no `Migration` class. A file called `operations.py` here
would break `migrate` on import.
"""

from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ProjectState

POLICY_SUFFIX = "_tenant_isolation"
PORTAL_CLIENT_POLICY_SUFFIX = "_portal_client_isolation"
PORTAL_DENY_POLICY_SUFFIX = "_portal_denied"
PORTAL_ROLE = "app_portal"
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

# Same three traps, one dimension down. The GUC differs (`app.client_id`) and the policy
# is RESTRICTIVE and scoped TO app_portal, so it ANDs with the permissive tenant policy
# above instead of replacing it — firm-side reads are untouched.
CLIENT_PREDICATE = "{column} = NULLIF(current_setting('app.client_id', true), '')::uuid"


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


class _PortalPolicy(Operation):
    """Shared machinery for the two `app_portal` restrictive policies.

    A RESTRICTIVE policy is AND-ed with the permissive tenant policy rather than
    replacing it, and applies only to the role named in its `TO` clause, so nothing
    here alters what `app_runtime` sees.
    """

    reversible = True
    reduces_to_sql = True

    def __init__(self, model_name: str, policy_suffix: str) -> None:
        self.model_name = model_name
        self.policy_suffix = policy_suffix

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """Serialize the operation back into migration source."""
        return (self.__class__.__qualname__, [], {"model_name": self.model_name})

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        """Change nothing: policies live in the database, not in migration state."""

    def _policy_name(self, table: str) -> str:
        name = f"{table}{self.policy_suffix}"
        if len(name) <= POSTGRES_IDENTIFIER_LIMIT:
            return name
        keep = POSTGRES_IDENTIFIER_LIMIT - len(self.policy_suffix)
        return f"{table[:keep]}{self.policy_suffix}"

    def _require_row_level_security(
        self,
        schema_editor: BaseDatabaseSchemaEditor,
        table: str,
    ) -> None:
        """Refuse to write a policy onto a table where RLS was never enabled.

        Such a policy is stored and never evaluated, so the portal reads every row
        while a coverage test that only checks for the policy's *existence* reports
        the table as covered. That combination shipped once in review and is the
        reason this guard exists rather than a comment.
        """
        if schema_editor.collect_sql:
            return
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(
                "SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass(%s)",
                [f"public.{table}"],
            )
            row = cursor.fetchone()
        if row is None or not row[0]:
            message = (
                f"{self.__class__.__name__} refuses {table}: row-level security is not "
                "enabled on it, so the policy would be stored and never evaluated. "
                "Either apply EnableRLS first, or add the table to "
                "PORTAL_DECISION_EXEMPT in apps/core/rls.py with a justification."
            )
            raise ImproperlyConfigured(message)

    def _policy_body(self, schema_editor: BaseDatabaseSchemaEditor) -> str:
        raise NotImplementedError

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        """Create the restrictive policy for `app_portal`."""
        model = to_state.apps.get_model(app_label, self.model_name)
        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return
        table = model._meta.db_table
        self._require_row_level_security(schema_editor, table)
        schema_editor.execute(
            f"CREATE POLICY {schema_editor.quote_name(self._policy_name(table))} "
            f"ON {schema_editor.quote_name(table)} "
            f"AS RESTRICTIVE FOR ALL TO {PORTAL_ROLE} "
            f"{self._policy_body(schema_editor)}",
            params=None,
        )

    def database_backwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        """Drop the restrictive policy, leaving the tenant policy untouched."""
        model = from_state.apps.get_model(app_label, self.model_name)
        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return
        table = model._meta.db_table
        schema_editor.execute(
            f"DROP POLICY IF EXISTS "
            f"{schema_editor.quote_name(self._policy_name(table))} "
            f"ON {schema_editor.quote_name(table)}",
            params=None,
        )


class EnablePortalClientRLS(_PortalPolicy):
    """Confine `app_portal` to the one client named by `app.client_id`.

    Tenant-level policies cannot express this: a portal user sits *inside* the firm's
    tenant, so `app.tenant_id` alone would show them every client of that firm.
    """

    def __init__(self, model_name: str, tenant_field: str = "client_id") -> None:
        super().__init__(model_name, PORTAL_CLIENT_POLICY_SUFFIX)
        self.tenant_field = tenant_field

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """Serialize the operation back into migration source."""
        kwargs: dict[str, Any] = {"model_name": self.model_name}
        if self.tenant_field != "client_id":
            kwargs["tenant_field"] = self.tenant_field
        return (self.__class__.__qualname__, [], kwargs)

    def _policy_body(self, schema_editor: BaseDatabaseSchemaEditor) -> str:
        predicate = CLIENT_PREDICATE.format(
            column=schema_editor.quote_name(self.tenant_field),
        )
        return f"USING ({predicate}) WITH CHECK ({predicate})"

    def describe(self) -> str:
        """Describe the operation for `migrate --plan` and `sqlmigrate`."""
        return f"Confine app_portal to app.client_id on {self.model_name}"

    @property
    def migration_name_fragment(self) -> str:
        """Supply the auto-generated migration filename fragment."""
        return f"portal_client_rls_{self.model_name.lower()}"


class DenyPortalAccess(_PortalPolicy):
    """Deny `app_portal` every row of a table the portal must never read."""

    def __init__(self, model_name: str) -> None:
        super().__init__(model_name, PORTAL_DENY_POLICY_SUFFIX)

    def _policy_body(self, schema_editor: BaseDatabaseSchemaEditor) -> str:
        return "USING (false) WITH CHECK (false)"

    def describe(self) -> str:
        """Describe the operation for `migrate --plan` and `sqlmigrate`."""
        return f"Deny app_portal all access to {self.model_name}"

    @property
    def migration_name_fragment(self) -> str:
        """Supply the auto-generated migration filename fragment."""
        return f"portal_deny_{self.model_name.lower()}"
