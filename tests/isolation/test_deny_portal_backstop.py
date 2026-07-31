"""The `DenyPortalAccess` policies refuse a read at runtime, proven without a grant.

Two tables carry `USING (false) WITH CHECK (false)` for `app_portal`: `audit_event` and
`clients_tag`. Neither is on the SELECT allow-list, so today the portal is stopped by
the missing grant and the policy is never consulted — it is a backstop, and one that
has never been exercised is a claim rather than a control.

**The naive version of this test is worthless, and that is why it is written this way.**
`USING (false)` on SELECT returns zero rows and never raises. So "the portal reads zero
rows" is equally true when:

* the grant is absent (the real reason today),
* the policy is absent,
* and the table is simply empty.

Three different states, one indistinguishable observation. The test therefore grants
SELECT inside a transaction, reads zero, then **drops the deny policy and reads again**,
and it is the second read returning rows that makes the first zero mean anything. The
whole thing rolls back, so no grant and no policy change survives the test.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from django.db import connection, transaction
from django.db.backends.utils import CursorWrapper

from apps.core import identifiers
from tests.isolation.rolecheck import assert_isolated_role

pytestmark = pytest.mark.django_db(transaction=True)

PORTAL_ROLE = "app_portal"

# table -> the INSERT that puts exactly one row in it, and the policy that denies the
# portal. Both tables are seeded here rather than through the ORM because the point is
# to observe the policy, not to exercise a model.
DENY_TABLES = {
    "clients_tag": (
        (
            "INSERT INTO clients_tag (id, name, created_at, tenant_id)"
            " VALUES (%s, 'probe', now(), %s)"
        ),
        "clients_tag_portal_denied",
    ),
    "audit_event": (
        (
            "INSERT INTO audit_event"
            " (id, action, object_type, object_id, metadata, created_at, tenant_id)"
            " VALUES (%s, 'probe.action', 'Probe', %s, '{}'::jsonb, now(), %s)"
        ),
        "audit_event_portal_denied",
    ),
}


def _seed(cursor: CursorWrapper, table: str, tenant_id: str) -> None:
    statement, _policy = DENY_TABLES[table]
    row_id = str(identifiers.uuid7())
    params = (
        [row_id, tenant_id]
        if table == "clients_tag"
        else [row_id, str(uuid4()), tenant_id]
    )
    cursor.execute(statement, params)


@pytest.fixture
def tenant_id() -> str:
    from apps.tenants.models import Tenant  # noqa: PLC0415

    tenant = Tenant.objects.create(
        name=f"Deny {datetime.now(tz=UTC):%H%M%S%f}",
        slug=f"deny-{uuid4().hex[:8]}",
    )
    return str(tenant.id)


@pytest.mark.parametrize("table", sorted(DENY_TABLES))
def test_the_deny_policy_refuses_the_read_and_the_control_proves_it(
    table: str,
    tenant_id: str,
) -> None:
    assert_isolated_role()
    _statement, policy = DENY_TABLES[table]

    with transaction.atomic(), connection.cursor() as cursor:
        # Given a row that exists, so "zero rows" cannot be about an empty table
        cursor.execute("RESET ROLE")
        _seed(cursor, table, tenant_id)
        cursor.execute(f"SELECT count(*) FROM {table}")  # noqa: S608
        assert cursor.fetchone()[0] == 1, "the seed did not land"

        # And a SELECT grant, so "zero rows" cannot be about a missing privilege.
        # Transactional: GRANT rolls back with everything else and leaves no residue.
        cursor.execute(f"GRANT SELECT ON {table} TO {PORTAL_ROLE}")

        # And the tenant GUC set, so the Phase-1 permissive tenant policy is SATISFIED.
        # Without it the row is filtered by tenant, the portal reads zero for that
        # reason, and dropping the deny policy below changes nothing -- which is exactly
        # how the first version of this test failed, and what its control exists to
        # catch.
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [tenant_id])

        # When the portal reads it
        cursor.execute(f"SET LOCAL ROLE {PORTAL_ROLE}")
        cursor.execute(f"SELECT count(*) FROM {table}")  # noqa: S608
        denied = cursor.fetchone()[0]

        # Then it sees nothing -- and USING (false) never raises, so this alone would
        # also pass with the policy deleted
        assert denied == 0

        # THE POSITIVE CONTROL. Drop the deny policy and repeat the identical query.
        cursor.execute("RESET ROLE")
        cursor.execute(f"DROP POLICY {policy} ON {table}")
        cursor.execute(f"SET LOCAL ROLE {PORTAL_ROLE}")
        cursor.execute(f"SELECT count(*) FROM {table}")  # noqa: S608
        without_policy = cursor.fetchone()[0]

        # Rows come back. THAT is what makes the zero above mean "the policy refused"
        # rather than "there was nothing to see".
        assert without_policy == 1, (
            f"{policy} was not what produced the zero; dropping it changed nothing, so "
            f"the refusal is coming from somewhere else"
        )

        transaction.set_rollback(True)


@pytest.mark.parametrize("table", sorted(DENY_TABLES))
def test_no_grant_or_policy_change_survived_the_transaction(table: str) -> None:
    # Given the previous test rolled back
    assert_isolated_role()
    _statement, policy = DENY_TABLES[table]

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT has_table_privilege(%s, %s, 'SELECT')",
            [PORTAL_ROLE, table],
        )
        assert cursor.fetchone()[0] is False, f"a SELECT grant on {table} leaked"

        cursor.execute(
            "SELECT count(*) FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid "
            "WHERE c.relname = %s AND p.polname = %s",
            [table, policy],
        )
        assert cursor.fetchone()[0] == 1, f"{policy} did not survive the rollback"
