"""Client-to-client denial for the portal role, proven at the database.

Phase-1 row-level security discriminates on `tenant_id` alone. A portal user sits
*inside* the accounting firm's tenant, so that layer alone would show them every client
of that firm — a competitor's tax filings included. The RESTRICTIVE policies scoped
`TO app_portal` add the client dimension, and this module is what proves they hold.

Every case uses a raw cursor under `SET LOCAL ROLE app_portal`. Written through the ORM
these would be satisfied by `TenantScopedManager`'s ContextVar filter even with the
portal policies dropped entirely, asserting nothing about the layer that actually holds
when application code is wrong.

`transaction.atomic()` is explicit in every case: `SET LOCAL` outside a transaction
emits a warning and silently does nothing, which would make every denial below pass for
the wrong reason.
"""

import datetime as dt
from decimal import Decimal

import pytest
from django.db import ProgrammingError, connection, transaction

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.obligations.models import MonthlyRevenue
from apps.tenants.models import Tenant
from tests.isolation.rolecheck import assert_isolated_role

pytestmark = pytest.mark.django_db(transaction=True)

ONBOARDING_ITEMS_PER_CLIENT = 6
REVENUES_PER_CLIENT = 1


class Clients:
    """Two MEI clients of the same accounting firm."""

    def __init__(
        self,
        tenant: Tenant,
        alpha: ClientCompany,
        beta: ClientCompany,
    ) -> None:
        self.tenant = tenant
        self.alpha = alpha
        self.beta = beta


@pytest.fixture
def clients() -> Clients:
    tenant = Tenant.objects.create(name="Firma", slug="firma")
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding needs both clients present, and the
        # denials below are precisely what this module asserts.
        alpha = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="CLIENTE ALPHA",
            cnpj="11222333000181",
            is_mei=True,
        )
        # ALL_OBJECTS_OK: the sibling this module proves alpha cannot read.
        beta = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="CLIENTE BETA",
            cnpj="11444777000161",
            is_mei=True,
        )
        for client in (alpha, beta):
            # ALL_OBJECTS_OK: as above.
            MonthlyRevenue.all_objects.create(
                tenant=tenant,
                client=client,
                competence_month=dt.date(2026, 1, 1),
                gross_amount=Decimal("1000.00"),
            )
    return Clients(tenant, alpha, beta)


def _portal_count(sql: str, tenant_id: str, client_id: str | None) -> int:
    """Count rows as `app_portal`, with the GUCs a portal request would carry."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE app_portal")
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [tenant_id])
        if client_id is not None:
            cursor.execute("SELECT set_config('app.client_id', %s, true)", [client_id])
        cursor.execute(sql)
        row = cursor.fetchone()
    return int(row[0]) if row else -1


def _portal_column(sql: str, tenant_id: str, client_id: str | None) -> list[str]:
    """Read one text column as `app_portal`, so denials assert identity not count."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE app_portal")
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [tenant_id])
        if client_id is not None:
            cursor.execute("SELECT set_config('app.client_id', %s, true)", [client_id])
        cursor.execute(sql)
        return [str(row[0]) for row in cursor.fetchall()]


def _portal_write(sql: str, tenant_id: str, client_id: str) -> None:
    """Attempt a write as `app_portal`."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE app_portal")
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [tenant_id])
        cursor.execute("SELECT set_config('app.client_id', %s, true)", [client_id])
        cursor.execute(sql)


def _runtime_count(sql: str, tenant_id: str) -> int:
    """Count rows as the firm-side role, with no client context at all."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [tenant_id])
        cursor.execute(sql)
        row = cursor.fetchone()
    return int(row[0]) if row else -1


def _runtime_counts_by_client(table: str, tenant_id: str) -> dict[str, int]:
    """Group rows per client as the firm-side role, for the positive control."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [tenant_id])
        cursor.execute(
            f"SELECT client_id, count(*) FROM {table} GROUP BY client_id",  # noqa: S608
        )
        return {str(row[0]): int(row[1]) for row in cursor.fetchall()}


COUNTS = [
    ("clients_onboardingitem", ONBOARDING_ITEMS_PER_CLIENT),
    ("obligations_monthlyrevenue", REVENUES_PER_CLIENT),
]


def test_1_the_seeding_actually_wrote_rows_for_both_clients(clients: Clients) -> None:
    # Given the fixture
    assert_isolated_role()
    tenant = str(clients.tenant.id)

    # When the firm-side role counts rows per client
    for table, per_client in COUNTS:
        counts = _runtime_counts_by_client(table, tenant)

        # Then BOTH clients have rows. This is the positive control for every denial
        # below: without it, "cannot see the other client" passes on an empty table.
        assert counts.get(str(clients.alpha.id)) == per_client, table
        assert counts.get(str(clients.beta.id)) == per_client, table


@pytest.mark.parametrize(("table", "per_client"), COUNTS)
def test_2_a_portal_client_reads_only_its_own_rows(
    clients: Clients,
    table: str,
    per_client: int,
) -> None:
    # Given a portal session scoped to alpha
    assert_isolated_role()

    # When it reads the table
    visible = _portal_column(
        f"SELECT DISTINCT client_id FROM {table}",  # noqa: S608
        str(clients.tenant.id),
        str(clients.alpha.id),
    )

    # Then only alpha's rows are visible, asserted by identity and not by count:
    # a policy returning the WRONG single client would satisfy a count-only check.
    assert visible == [str(clients.alpha.id)]
    assert per_client > 0


@pytest.mark.parametrize(("table", "per_client"), COUNTS)
def test_3_a_portal_client_cannot_read_a_sibling_by_explicit_id(
    clients: Clients,
    table: str,
    per_client: int,
) -> None:
    # Given a portal session scoped to alpha
    assert_isolated_role()
    assert per_client > 0

    # When it asks for beta's rows by primary key
    visible = _portal_count(
        f"SELECT count(*) FROM {table} WHERE client_id = '{clients.beta.id}'",  # noqa: S608
        str(clients.tenant.id),
        str(clients.alpha.id),
    )

    # Then none come back
    assert visible == 0


@pytest.mark.parametrize(("table", "per_client"), COUNTS)
def test_4_a_portal_session_with_no_client_guc_reads_nothing(
    clients: Clients,
    table: str,
    per_client: int,
) -> None:
    # Given a portal session that never set app.client_id
    assert_isolated_role()
    assert per_client > 0

    # When it reads the table
    visible = _portal_count(
        f"SELECT count(*) FROM {table}",  # noqa: S608
        str(clients.tenant.id),
        None,
    )

    # Then the comparison against NULL yields no rows: fail-closed, not fail-open
    assert visible == 0


@pytest.mark.parametrize(("table", "per_client"), COUNTS)
def test_5_an_empty_client_guc_reads_nothing(
    clients: Clients,
    table: str,
    per_client: int,
) -> None:
    # Given a portal session whose GUC is the empty string, as a recycled connection
    # or a role-level default would leave it
    assert_isolated_role()
    assert per_client > 0

    # When it reads the table
    visible = _portal_count(
        f"SELECT count(*) FROM {table}",  # noqa: S608
        str(clients.tenant.id),
        "",
    )

    # Then NULLIF folds it to NULL and nothing matches
    assert visible == 0


def test_6_the_portal_sees_exactly_its_own_client_company(clients: Clients) -> None:
    # Given a portal session scoped to alpha
    assert_isolated_role()

    # When it reads the client registry, whose identity column is `id`, not `client_id`
    visible = _portal_column(
        "SELECT legal_name FROM clients_clientcompany",
        str(clients.tenant.id),
        str(clients.alpha.id),
    )

    # Then exactly its own company comes back, by name
    assert visible == ["CLIENTE ALPHA"]


def test_7_the_portal_cannot_write(clients: Clients) -> None:
    # Given a portal session scoped to alpha
    assert_isolated_role()

    # When it attempts to insert a row for itself
    # Then it is refused. In Phase 2a the refusal is a PRIVILEGE error, not a policy
    # violation: app_portal holds SELECT only, and the privilege check fires before
    # row-level security is consulted. Phase 2b widens the grant for document upload,
    # at which point the WITH CHECK half of the policy becomes the control instead.
    with pytest.raises(ProgrammingError, match="permission denied"):
        _portal_write(
            "INSERT INTO obligations_monthlyrevenue "  # noqa: S608
            "(id, tenant_id, client_id, competence_month, gross_amount) "
            f"VALUES (gen_random_uuid(), '{clients.tenant.id}', "
            f"'{clients.alpha.id}', '2026-02-01', 1)",
            str(clients.tenant.id),
            str(clients.alpha.id),
        )


def test_8_the_firm_side_role_is_unaffected_by_the_portal_policies(
    clients: Clients,
) -> None:
    # Given the firm-side role with no client context whatsoever
    assert_isolated_role()

    # When it reads a table the portal policies now police
    visible = _runtime_count(
        "SELECT count(*) FROM clients_onboardingitem",
        str(clients.tenant.id),
    )

    # Then it still sees BOTH clients' rows.
    #
    # This is the regression test for the single most dangerous defect in this design:
    # PostgreSQL matches a policy's TO clause by privilege INHERITANCE, not identity.
    # Granting app_portal to app_runtime without WITH INHERIT FALSE makes these
    # RESTRICTIVE policies bind app_runtime too, and the accounting firm reads zero
    # rows from every client table.
    assert visible == ONBOARDING_ITEMS_PER_CLIENT * 2


def test_9_the_role_reverts_when_the_transaction_ends(clients: Clients) -> None:
    # Given a portal session
    assert_isolated_role()
    _portal_count(
        "SELECT count(*) FROM clients_onboardingitem",
        str(clients.tenant.id),
        str(clients.alpha.id),
    )

    # When the transaction has committed
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        row = cursor.fetchone()

    # Then the connection is back to the firm-side role, so a pooled connection
    # cannot leak portal scope into the next request
    assert row is not None
    assert row[0] == "app_runtime"
