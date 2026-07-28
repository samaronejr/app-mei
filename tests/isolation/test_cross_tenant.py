"""Cross-tenant denial, proven at the database rather than asserted at the ORM.

Every denial here goes through `all_objects` or a raw cursor. Written as
`Model.objects...` these cases would be satisfied by `TenantScopedManager`'s ContextVar
filter **even with row-level security switched off entirely** — proving layer 1 and
asserting nothing whatsoever about layer 2, which is the layer that actually holds when
application code is wrong.

`transaction=True` throughout: under a plain `TestCase` the whole test already runs
inside a transaction, so `SET LOCAL` values would persist to the enclosing transaction
on savepoint RELEASE instead of being discarded.
"""

from http import HTTPStatus

import pytest
import uuid6
from django.db import ProgrammingError, connection, transaction
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.core.tests.models import ExampleTenantModel
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.isolation.rolecheck import assert_isolated_role
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

TABLE = ExampleTenantModel._meta.db_table
ROWS_FOR_ALPHA = 3
ROWS_FOR_BETA = 2


class Tenants:
    """The two tenants every case below contrasts."""

    def __init__(self, alpha: Tenant, beta: Tenant) -> None:
        self.alpha = alpha
        self.beta = beta


@pytest.fixture
def tenants() -> Tenants:
    alpha = Tenant.objects.create(name="Alpha", slug="alpha")
    beta = Tenant.objects.create(name="Beta", slug="beta")
    for tenant, count in ((alpha, ROWS_FOR_ALPHA), (beta, ROWS_FOR_BETA)):
        with tenant_context(tenant.id):
            for index in range(count):
                ExampleTenantModel.all_objects.create(
                    tenant=tenant,
                    name=f"{tenant.slug}-{index}",
                )
    return Tenants(alpha, beta)


def _raw_count(where: str = "", params: list[str] | None = None) -> int:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM {TABLE} {where}", params or [])  # noqa: S608
        row = cursor.fetchone()
    return int(row[0]) if row else -1


def test_the_seeding_actually_wrote_rows(tenants: Tenants) -> None:
    # Given the fixture, read back under each tenant's own context
    assert_isolated_role()

    # When each tenant counts its own rows
    with tenant_context(tenants.alpha.id):
        alpha_visible = _raw_count()
    with tenant_context(tenants.beta.id):
        beta_visible = _raw_count()

    # Then both are non-zero. This is the positive control for every denial below:
    # without it, "cannot see the other tenant" would pass on an empty table.
    assert alpha_visible == ROWS_FOR_ALPHA
    assert beta_visible == ROWS_FOR_BETA
    assert alpha_visible > 0
    assert beta_visible > 0


def test_case_1_tenant_a_cannot_read_tenant_b_rows(tenants: Tenants) -> None:
    # Given tenant Alpha in context, with Beta's rows present in the same table
    assert_isolated_role()

    with tenant_context(tenants.alpha.id):
        # When Beta's rows are asked for directly, bypassing the ORM manager
        beta_rows = _raw_count("WHERE tenant_id = %s", [str(tenants.beta.id)])
        total_visible = _raw_count()
        via_unscoped_manager = ExampleTenantModel.all_objects.filter(
            tenant_id=tenants.beta.id,
        ).count()

    # CI GATE PROOF (throwaway branch): asserts cross-tenant LEAKAGE instead of
    # denial, i.e. exactly the shape an isolation regression takes.
    assert beta_rows == ROWS_FOR_BETA
    assert via_unscoped_manager == ROWS_FOR_BETA
    assert total_visible == ROWS_FOR_ALPHA + ROWS_FOR_BETA


def test_case_2_no_context_returns_zero_rows_when_the_guc_is_absent(
    tenants: Tenants,
) -> None:
    # Given a connection that has never set the GUC, so the setting is genuinely
    # ABSENT. PostgreSQL creates the `app.tenant_id` placeholder on the first SET and
    # it reads back as '' from then on, so the absent shape only exists on a fresh
    # connection — which is exactly what a worker picking one out of the pool has.
    assert tenants.alpha is not None
    connection.close()
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE app_runtime")
    assert_isolated_role()

    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.tenant_id', true) IS NULL")
        row = cursor.fetchone()
    assert row is not None
    assert row[0] is True, "this case must exercise the ABSENT-GUC shape"

    # When the table is read
    visible = _raw_count()
    via_unscoped_manager = ExampleTenantModel.all_objects.count()

    # Then nothing is visible — the fail-closed default
    assert visible == 0
    assert via_unscoped_manager == 0


def test_case_2_no_context_returns_zero_rows_when_the_guc_is_empty(
    tenants: Tenants,
) -> None:
    # Given the GUC set to the EMPTY STRING, which is what every production login
    # carries via `ALTER ROLE app_runtime SET app.tenant_id TO ''`
    assert_isolated_role()
    assert tenants.alpha is not None

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.tenant_id', '', true)")

        # When the table is read
        visible = _raw_count()
        via_unscoped_manager = ExampleTenantModel.all_objects.count()

    # Then nothing is visible here either. Only asserting the absent shape would leave
    # the production path — this one — completely unexercised.
    assert visible == 0
    assert via_unscoped_manager == 0


def test_case_3_with_check_refuses_a_write_into_another_tenant(
    tenants: Tenants,
) -> None:
    # Given tenant Alpha in context
    assert_isolated_role()

    # When a row carrying Beta's tenant_id is inserted.
    # Then WITH CHECK refuses it. USING alone would have allowed this write.
    #
    # The atomic() is required, not decorative: swallowing a DatabaseError leaves the
    # transaction aborted, and Django forbids further queries on it. The savepoint
    # contains the rollback so the enclosing tenant context stays usable.
    with (
        tenant_context(tenants.alpha.id),
        pytest.raises(ProgrammingError, match="row-level security policy"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            f"INSERT INTO {TABLE} (id, name, tenant_id) VALUES (%s, %s, %s)",  # noqa: S608
            [str(uuid6.uuid7()), "smuggled", str(tenants.beta.id)],
        )


def test_case_3_with_check_refuses_moving_a_row_to_another_tenant(
    tenants: Tenants,
) -> None:
    # Given tenant Alpha in context and one of Alpha's own rows
    assert_isolated_role()

    # When that row is updated to belong to Beta
    # Then WITH CHECK refuses that too — a policy guarding only INSERT would let a
    # tenant hand its own row over to another tenant
    with (
        tenant_context(tenants.alpha.id),
        pytest.raises(ProgrammingError, match="row-level security policy"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            f"UPDATE {TABLE} SET tenant_id = %s",  # noqa: S608
            [str(tenants.beta.id)],
        )


def test_case_4_http_request_for_another_tenants_object_is_refused(
    client: Client,
    tenants: Tenants,
    settings: SettingsWrapper,
) -> None:
    # Given a user who belongs only to Alpha, and a row that belongs to Beta
    settings.ROOT_URLCONF = "tests.isolation.urls"
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]

    member = User.objects.create_user(email="member@alpha.example")
    Membership.objects.create(
        user=member,
        tenant=tenants.alpha,
        role=TenantRole.OWNER,
    )
    enrol_totp(member)
    with tenant_context(tenants.beta.id):
        beta_row = ExampleTenantModel.all_objects.filter(
            tenant_id=tenants.beta.id,
        ).first()
    assert beta_row is not None

    # When they request Beta's object from Alpha's subdomain
    client.force_login(member)
    response = client.get(f"/rows/{beta_row.pk}/", headers={"host": "alpha.localhost"})

    # Then the object is never disclosed
    assert response.status_code in {HTTPStatus.NOT_FOUND, HTTPStatus.FORBIDDEN}
    assert beta_row.name.encode() not in response.content


def test_case_4_http_request_for_its_own_object_succeeds(
    client: Client,
    tenants: Tenants,
    settings: SettingsWrapper,
) -> None:
    # Given the same user requesting one of ALPHA's rows
    settings.ROOT_URLCONF = "tests.isolation.urls"
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]

    member = User.objects.create_user(email="member2@alpha.example")
    Membership.objects.create(
        user=member,
        tenant=tenants.alpha,
        role=TenantRole.OWNER,
    )
    enrol_totp(member)
    with tenant_context(tenants.alpha.id):
        alpha_row = ExampleTenantModel.all_objects.filter(
            tenant_id=tenants.alpha.id,
        ).first()
    assert alpha_row is not None

    # When it is requested
    client.force_login(member)
    response = client.get(f"/rows/{alpha_row.pk}/", headers={"host": "alpha.localhost"})

    # Then it IS served. Without this control, case 4 would pass on a route that
    # returns 404 for absolutely everything.
    assert response.status_code == HTTPStatus.OK
    assert response.content.decode() == alpha_row.name
