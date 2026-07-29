"""The portal migration operations must emit the exact policy shape, or emit nothing.

Two failure modes are specific to these operations and neither is visible at runtime in
Phase 2a, because `app_portal` holds SELECT on six tables only and the privilege check
fires before row-level security is ever consulted:

1. A policy written onto a table where RLS was never enabled is *stored and never
   evaluated*. The portal then reads every row while a coverage test that checks only
   for the policy's existence reports the table as covered.
2. A predicate that compares the wrong column still contains every substring a loose
   assertion looks for — `client_id` occurs inside the GUC literal
   `current_setting('app.client_id', ...)`, and `id` is a substring of `tenant_id`.

So the assertions below anchor on the left operand rather than searching for it.
"""

import pytest
from django.apps import apps as django_apps
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.db.migrations.state import ProjectState

from apps.core.migrations._operations import (
    PORTAL_ROLE,
    DenyPortalAccess,
    EnablePortalClientRLS,
    _PortalPolicy,
)
from tests.isolation.rolecheck import assert_isolated_role

pytestmark = pytest.mark.django_db(transaction=True)


def _generated_sql(operation: _PortalPolicy, app_label: str) -> str:
    state = ProjectState.from_apps(django_apps)
    with connection.schema_editor(collect_sql=True, atomic=False) as editor:
        operation.database_forwards(app_label, editor, state, state)
        return "\n".join(editor.collected_sql)


def test_the_client_policy_is_restrictive_and_scoped_to_the_portal_role() -> None:
    # Given the operation that confines app_portal to one client
    assert_isolated_role()

    # When its SQL is generated
    sql = _generated_sql(EnablePortalClientRLS("Obligation"), "obligations")

    # Then it is RESTRICTIVE, so it ANDs with the permissive tenant policy
    assert "AS RESTRICTIVE" in sql
    # And it binds only the portal role, so firm-side reads are untouched
    assert f"TO {PORTAL_ROLE}" in sql
    # And it covers writes as well as reads
    assert "FOR ALL" in sql
    assert "WITH CHECK" in sql


def test_the_client_predicate_keeps_every_fail_closed_trap() -> None:
    # Given the same operation
    assert_isolated_role()

    # When its SQL is generated
    sql = _generated_sql(EnablePortalClientRLS("Obligation"), "obligations")

    # Then current_setting is missing_ok, or an unset GUC raises instead of denying
    assert ", true)" in sql
    # And the empty string is folded to NULL, or ''::uuid raises SQLSTATE 22P02
    assert "NULLIF(" in sql
    # And the GUC is the client one, not a copy-pasted tenant predicate
    assert "app.client_id" in sql
    assert "app.tenant_id" not in sql


@pytest.mark.parametrize(
    ("model_name", "app_label", "column"),
    [
        ("Obligation", "obligations", "client_id"),
        ("MonthlyRevenue", "obligations", "client_id"),
        ("ClientAssignment", "clients", "client_id"),
        ("ClientCompany", "clients", "id"),
    ],
)
def test_the_predicate_anchors_on_the_expected_column(
    model_name: str,
    app_label: str,
    column: str,
) -> None:
    # Given a client-scoped model whose identity column is known
    assert_isolated_role()
    field = "id" if column == "id" else "client_id"

    # When the policy SQL is generated
    sql = _generated_sql(
        EnablePortalClientRLS(model_name, tenant_field=field), app_label
    )

    # Then the column is the LEFT operand of the comparison, not merely present
    # somewhere in the string. A substring test passes for a wrong-column predicate
    # because the GUC literal already contains "client_id".
    assert f'USING ("{column}" = NULLIF(' in sql
    assert f'WITH CHECK ("{column}" = NULLIF(' in sql


def test_a_wrong_column_predicate_would_not_satisfy_the_anchor() -> None:
    # Given a deliberately wrong operation comparing tenant_id against app.client_id
    assert_isolated_role()

    # When its SQL is generated
    sql = _generated_sql(
        EnablePortalClientRLS("Obligation", tenant_field="tenant_id"),
        "obligations",
    )

    # Then a loose substring check would pass, which is why it must not be used
    assert "client_id" in sql

    # And the anchored check rejects it, which is the assertion that has teeth
    assert 'USING ("client_id" = NULLIF(' not in sql


def test_the_deny_policy_denies_every_row_in_both_directions() -> None:
    # Given the operation that shuts the portal out of a table entirely
    assert_isolated_role()

    # When its SQL is generated
    sql = _generated_sql(DenyPortalAccess("Event"), "audit")

    # Then reads and writes are both refused for the portal role only
    assert "AS RESTRICTIVE" in sql
    assert f"TO {PORTAL_ROLE}" in sql
    assert "USING (false)" in sql
    assert "WITH CHECK (false)" in sql


def test_the_two_operations_use_distinct_policy_names() -> None:
    # Given one table policed each way
    assert_isolated_role()

    # When the policy names are derived
    client_policy = EnablePortalClientRLS("Obligation")._policy_name("t")
    deny_policy = DenyPortalAccess("Event")._policy_name("t")

    # Then they differ, so the coverage meta-test can tell the two branches apart
    assert client_policy != deny_policy


def test_a_policy_is_refused_on_a_table_where_rls_was_never_enabled() -> None:
    # Given audit_accesslog, which carries tenant_id but is deliberately unpoliced
    assert_isolated_role()
    state = ProjectState.from_apps(django_apps)

    # When a portal policy is applied to it for real, not collected as SQL
    with (
        pytest.raises(ImproperlyConfigured, match="row-level security is not enabled"),
        connection.schema_editor(atomic=False) as editor,
    ):
        DenyPortalAccess("AccessLog").database_forwards("audit", editor, state, state)

    # Then the operation refuses, because such a policy is stored and never evaluated


def test_the_rls_guard_is_skipped_when_only_collecting_sql() -> None:
    # Given the same refused table
    assert_isolated_role()

    # When SQL is merely collected, as sqlmigrate does against any database
    sql = _generated_sql(DenyPortalAccess("AccessLog"), "audit")

    # Then no query runs and the SQL is still produced, so sqlmigrate never
    # raises spuriously on a database where the table does not yet exist
    assert "AS RESTRICTIVE" in sql
