"""A denied action must still be on record after the request that denied it failed.

`ATOMIC_REQUESTS` makes the view callable a savepoint, and `PermissionDenied` converts
to a 403 rather than a 500 — so the savepoint rolls back and takes every write the
view made with it. The audit rows most worth keeping are written by exactly those
requests.

The contrast below is the whole point: after one request, the tenant-scoped `Event` is
gone (the rollback worked) and the `PlatformEvent` is still there (it escaped). Either
assertion alone would be satisfiable by a broken system — the first by an audit layer
that never writes anything, the second by a transaction that never rolls back.
"""

from http import HTTPStatus

import pytest
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.audit.models import AuditAction, Event, PlatformEvent
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.audit.urls import DENIED_SUBJECT
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

TENANT_HOST = "alpha.localhost"


@pytest.fixture(autouse=True)
def _urls(settings: SettingsWrapper) -> None:
    settings.ROOT_URLCONF = "tests.audit.urls"
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]


@pytest.fixture
def tenant() -> Tenant:
    return Tenant.objects.create(name="Alpha", slug="alpha")


@pytest.fixture
def member(tenant: Tenant) -> User:
    user = User.objects.create_user(email="owner@alpha.example")
    Membership.objects.create(user=user, tenant=tenant, role=TenantRole.OWNER)
    enrol_totp(user)
    return user


def _counts(tenant: Tenant) -> tuple[int, int]:
    with tenant_context(tenant.id):
        events = Event.objects.filter(action=AuditAction.EXPORT).count()
    denials = PlatformEvent.objects.filter(subject=DENIED_SUBJECT).count()
    return events, denials


def test_a_successful_request_commits_both_kinds_of_event(
    client: Client,
    tenant: Tenant,
    member: User,
) -> None:
    # Given a member on their firm's subdomain
    client.force_login(member)

    # When a view audits and returns normally
    response = client.get("/audit-ok/", headers={"host": TENANT_HOST})

    # Then both rows are on disk. This is the positive control for the case below.
    assert response.status_code == HTTPStatus.OK
    assert _counts(tenant) == (1, 1)


def test_a_denied_request_rolls_back_the_tenant_event_but_keeps_the_platform_event(
    client: Client,
    tenant: Tenant,
    member: User,
) -> None:
    # Given the same member
    client.force_login(member)

    # When the view audits and then raises PermissionDenied — which converts to 403,
    # not 500, so a `status_code >= 500` check would have missed this entirely
    response = client.get("/audit-deny/", headers={"host": TENANT_HOST})
    assert response.status_code == HTTPStatus.FORBIDDEN

    # Then the tenant-scoped event was rolled back with the rest of the view's work...
    events, denials = _counts(tenant)
    assert events == 0, "the ATOMIC_REQUESTS savepoint did not roll back"

    # ...and the platform event survived, because it is flushed by middleware sitting
    # OUTSIDE the tenant transaction. This is the record an investigation needs.
    assert denials == 1, (
        "the denial was erased by the rollback — platform events must be flushed "
        "outside the view transaction"
    )
