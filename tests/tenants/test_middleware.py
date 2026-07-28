"""The middleware must hold tenant context across the view AND rendering, then drop it.

Every test here uses `transaction=True`. Under a plain `TestCase` the whole test
already runs inside a transaction, so the middleware's `atomic()` would degrade to a
savepoint — and `SET LOCAL` values persist to the *enclosing* transaction on savepoint
RELEASE rather than being discarded. The "GUC is empty afterwards" assertions would
then fail, or worse pass for the wrong reason while state leaked between tests.
"""

from http import HTTPStatus

import pytest
from django.db import connection, transaction
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.core.tenancy import current_tenant_id
from apps.core.tests.models import ExampleTenantModel
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _tenant_urls(settings: SettingsWrapper) -> None:
    settings.ROOT_URLCONF = "tests.tenants.urls"
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]


ALPHA_HOST = "alpha.localhost"
BETA_HOST = "beta.localhost"
ROWS_FOR_ALPHA = 2


def _read_guc() -> str:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT coalesce(current_setting('app.tenant_id', true), 'NULL')",
        )
        row = cursor.fetchone()
    return str(row[0]) if row else ""


def _committed_rows() -> int:
    """Count rows as they exist on disk, ignoring row-level security.

    Read as `app_test`, which holds BYPASSRLS, so this answers "did it commit?" rather
    than "is it visible?" — otherwise an uncommitted row and an invisible one would be
    indistinguishable and the rollback assertions would pass vacuously.
    """
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        cursor.execute("SELECT count(*) FROM core_tests_exampletenantmodel")
        row = cursor.fetchone()
        cursor.execute("SET ROLE app_runtime")
    return int(row[0]) if row else -1


@pytest.fixture
def alpha() -> Tenant:
    return Tenant.objects.create(name="Alpha", slug="alpha")


@pytest.fixture
def beta() -> Tenant:
    return Tenant.objects.create(name="Beta", slug="beta")


@pytest.fixture
def alpha_member(alpha: Tenant) -> User:
    user = User.objects.create_user(email="member@alpha.example")
    Membership.objects.create(user=user, tenant=alpha, role=TenantRole.OWNER)
    enrol_totp(user)
    return user


@pytest.fixture
def alpha_rows(alpha: Tenant) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(alpha.id)])
        for index in range(ROWS_FOR_ALPHA):
            # ALL_OBJECTS_OK: fixture seeding under a raw GUC, before the request
            # that this test exists to observe has set any ContextVar.
            ExampleTenantModel.all_objects.create(tenant=alpha, name=f"row{index}")


def test_the_guc_holds_the_resolved_tenant_during_view_execution(
    client: Client,
    alpha: Tenant,
    alpha_member: User,
) -> None:
    # Given a member of tenant Alpha on Alpha's subdomain
    client.force_login(alpha_member)

    # When a view reads the GUC straight from the database
    response = client.get("/guc/", headers={"host": ALPHA_HOST})

    # Then it already holds Alpha's id
    assert response.status_code == HTTPStatus.OK
    assert response.content.decode() == str(alpha.id)


def test_a_lazy_queryset_still_resolves_during_template_rendering(
    client: Client,
    alpha: Tenant,
    alpha_member: User,
    alpha_rows: None,
) -> None:
    # Given rows for Alpha and a view returning an unevaluated queryset in its context
    client.force_login(alpha_member)

    # When the template renders it
    response = client.get("/lazy/", headers={"host": ALPHA_HOST})
    body = response.content.decode()

    # Then the rows are present. This is the regression test for rendering outside the
    # transaction, which would produce an empty page rather than an error.
    assert response.status_code == HTTPStatus.OK
    assert body.count("[") == ROWS_FOR_ALPHA
    assert "[row0]" in body
    assert alpha_rows is None


def test_the_guc_is_empty_again_after_the_response(
    client: Client,
    alpha_member: User,
) -> None:
    # Given a completed tenant-scoped request
    client.force_login(alpha_member)
    client.get("/guc/", headers={"host": ALPHA_HOST})

    # When the connection is inspected afterwards
    # Then no tenant remains pinned to it
    assert _read_guc() in {"", "NULL"}


def test_an_anonymous_request_carries_no_tenant(client: Client, alpha: Tenant) -> None:
    # Given no authenticated user, on a tenant subdomain
    # When a view reads the GUC
    response = client.get("/guc/", headers={"host": ALPHA_HOST})

    # Then it is empty rather than the resolved tenant, and certainly not stale
    assert response.content.decode() in {"", "NULL"}
    assert alpha.slug == "alpha"


def test_an_anonymous_request_does_not_inherit_the_previous_tenant(
    client: Client,
    alpha_member: User,
) -> None:
    # Given an authenticated tenant request has already run on this connection
    client.force_login(alpha_member)
    first = client.get("/guc/", headers={"host": ALPHA_HOST})
    assert first.content.decode() != ""

    # When an anonymous request follows on the same connection
    anonymous = Client()
    second = anonymous.get("/guc/", headers={"host": "localhost"})

    # Then it sees no tenant, not the previous request's one
    assert second.content.decode() in {"", "NULL"}


def test_membership_is_readable_with_no_tenant_context(alpha_member: User) -> None:
    # Given no tenant context whatsoever
    assert current_tenant_id.get() is None
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id', '', true)")

        # When the login path resolves the user's memberships
        found = Membership.objects.filter(user=alpha_member).count()

    # Then it works. This is the bootstrap read the middleware itself depends on.
    assert found == 1


def test_the_context_var_does_not_leak_between_requests(
    client: Client,
    alpha_member: User,
) -> None:
    # Given a request served as tenant Alpha on this thread
    client.force_login(alpha_member)
    client.get("/guc/", headers={"host": ALPHA_HOST})

    # When it has finished
    # Then the ContextVar is unset, not still holding Alpha
    assert current_tenant_id.get() is None

    # And an anonymous request afterwards leaves it unset too
    Client().get("/guc/", headers={"host": "localhost"})
    assert current_tenant_id.get() is None


def test_a_user_without_membership_is_refused(
    client: Client,
    alpha: Tenant,
    beta: Tenant,
) -> None:
    # Given a user who belongs to Beta but not to Alpha
    outsider = User.objects.create_user(email="outsider@beta.example")
    Membership.objects.create(user=outsider, tenant=beta, role=TenantRole.OWNER)
    enrol_totp(outsider)
    client.force_login(outsider)

    # When they request Alpha's subdomain
    response = client.get("/guc/", headers={"host": ALPHA_HOST})

    # Then they are refused rather than served an empty page
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert alpha.slug == "alpha"


def test_an_inactive_membership_is_refused(
    client: Client,
    alpha: Tenant,
) -> None:
    # Given a membership that has been deactivated
    former = User.objects.create_user(email="former@alpha.example")
    Membership.objects.create(
        user=former,
        tenant=alpha,
        role=TenantRole.OWNER,
        is_active=False,
    )
    client.force_login(former)

    # When they request the subdomain
    response = client.get("/guc/", headers={"host": ALPHA_HOST})

    # Then access is refused
    assert response.status_code == HTTPStatus.FORBIDDEN


def test_a_streaming_response_under_a_tenant_is_refused(
    client: Client,
    alpha_member: User,
    alpha_rows: None,
) -> None:
    # Given a tenant-scoped view that returns a streaming response
    client.force_login(alpha_member)

    # When it is requested
    # Then the middleware refuses it, rather than letting the WSGI server consume the
    # iterator after the transaction has closed and deliver an empty file
    with pytest.raises(TypeError, match="streaming response"):
        client.get("/streaming/", headers={"host": ALPHA_HOST})
    assert alpha_rows is None


def test_a_successful_view_commits_its_write(
    client: Client,
    alpha_member: User,
) -> None:
    # Given a view that writes a row and returns normally
    client.force_login(alpha_member)
    before = _committed_rows()

    # When it is requested
    response = client.get("/write-ok/", headers={"host": ALPHA_HOST})

    # Then the row is committed. This is the positive control: without it the rollback
    # assertions below could pass because nothing was ever written at all.
    assert response.status_code == HTTPStatus.OK
    assert _committed_rows() == before + 1


def test_a_view_raising_an_exception_commits_nothing(
    client: Client,
    alpha_member: User,
) -> None:
    # Given a view that writes a row and then raises
    client.force_login(alpha_member)
    before = _committed_rows()

    # When it is requested
    with pytest.raises(ValueError, match="deliberate failure"):
        client.get("/write-boom/", headers={"host": ALPHA_HOST})

    # Then the partial write was rolled back by the ATOMIC_REQUESTS savepoint
    assert _committed_rows() == before


@pytest.mark.parametrize(
    ("url", "expected_status"),
    [("/write-404/", HTTPStatus.NOT_FOUND), ("/write-403/", HTTPStatus.FORBIDDEN)],
)
def test_a_view_converting_to_4xx_commits_nothing(
    client: Client,
    alpha_member: User,
    url: str,
    expected_status: HTTPStatus,
) -> None:
    # Given a view that writes a row and raises Http404 or PermissionDenied
    client.force_login(alpha_member)
    before = _committed_rows()

    # When it is requested
    response = client.get(url, headers={"host": ALPHA_HOST})

    # Then it converts to 4xx AND commits nothing. A `status_code >= 500` guard would
    # have missed exactly this case and committed the partial write.
    assert response.status_code == expected_status
    assert _committed_rows() == before
