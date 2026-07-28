"""The Marco Civil access log: written for every request, purged at six months.

The ordering assertion is the one that matters. Registered inside the tenant
transaction, this middleware would have its row rolled back whenever the request
failed — which is to say, the access record would be missing for exactly the requests
an intrusion investigation starts from, and present for all the boring ones.
"""

from datetime import timedelta
from http import HTTPStatus

import pytest
from django.conf import settings
from django.test import Client
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.audit.models import AccessLog
from apps.audit.tasks import purge_access_logs
from apps.core.rls import is_exempt_from_tenant_policy
from apps.tenants.models import Membership, Tenant, TenantRole
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


def test_an_authenticated_request_is_logged_with_its_tenant(
    client: Client,
    tenant: Tenant,
    member: User,
) -> None:
    # Given a member on their firm's subdomain
    client.force_login(member)

    # When they make a request
    client.get("/audit-ok/", headers={"host": TENANT_HOST})

    # Then exactly one record carries the resolved tenant and the acting user
    row = AccessLog.objects.get(path="/audit-ok/")
    assert row.tenant_id == tenant.pk
    assert row.user_id == member.pk
    assert row.method == "GET"
    assert row.status_code == HTTPStatus.OK


def test_an_anonymous_request_is_logged_with_a_null_tenant(client: Client) -> None:
    # Given no session
    # When a request is made against the platform host
    client.get("/ping/")

    # Then it is still recorded — a fail-closed policy would have refused this insert,
    # losing exactly the pre-authentication records an investigation begins with
    row = AccessLog.objects.get(path="/ping/")
    assert row.tenant_id is None
    assert row.user_id is None


def test_a_failed_request_is_still_logged(
    client: Client,
    tenant: Tenant,
    member: User,
) -> None:
    # Given a member
    client.force_login(member)

    # When the request is refused after the view raised
    response = client.get("/audit-deny/", headers={"host": TENANT_HOST})
    assert response.status_code == HTTPStatus.FORBIDDEN

    # Then the access record survives the rollback that erased the view's own writes.
    # This is the whole reason the middleware sits outside the tenant transaction.
    row = AccessLog.objects.get(path="/audit-deny/")
    assert row.status_code == HTTPStatus.FORBIDDEN
    assert row.tenant_id == tenant.pk


def test_a_request_refused_by_tenant_resolution_is_still_logged(
    client: Client,
    tenant: Tenant,
) -> None:
    # Given an authenticated user with no membership for this firm
    outsider = User.objects.create_user(email="fora@outra.example")
    other = Tenant.objects.create(name="Beta", slug="beta")
    Membership.objects.create(user=outsider, tenant=other, role=TenantRole.OWNER)
    enrol_totp(outsider)
    client.force_login(outsider)

    # When they probe another firm's subdomain and TenantMiddleware refuses them
    response = client.get("/audit-ok/", headers={"host": TENANT_HOST})
    assert response.status_code == HTTPStatus.FORBIDDEN

    # Then the probe is on record. TenantMiddleware raises BEFORE it calls the rest of
    # the chain, so an access log registered inside it would never run at all — every
    # membership-denied request, which is the shape tenant probing takes, would go
    # entirely unlogged.
    row = AccessLog.objects.get(path="/audit-ok/")
    assert row.status_code == HTTPStatus.FORBIDDEN
    assert row.user_id == outsider.pk
    assert row.tenant_id is None
    assert tenant.slug == "alpha"


def test_the_access_log_middleware_runs_outside_the_tenant_transaction() -> None:
    # Given the shipped middleware stack
    middleware = list(settings.MIDDLEWARE)

    # When the two positions are compared
    access = middleware.index("apps.audit.middleware.AccessLogMiddleware")
    tenant_mw = middleware.index("apps.tenants.middleware.TenantMiddleware")

    # Then the access log is outer, so its write is never inside the rolled-back
    # transaction. core.E007 fails startup if this is ever reversed.
    assert access < tenant_mw


def test_the_access_log_table_is_allow_listed() -> None:
    # Given the row-level-security allow-list
    # When the access-log table is classified
    # Then it is exempt: a fail-closed policy would refuse anonymous requests
    assert is_exempt_from_tenant_policy("audit_accesslog")


def _seed_aged(days: int, path: str) -> AccessLog:
    row = AccessLog.objects.create(method="GET", path=path, status_code=200)
    AccessLog.objects.filter(pk=row.pk).update(
        created_at=timezone.now() - timedelta(days=days),
    )
    return row


def test_the_purge_deletes_past_retention_and_keeps_within_it() -> None:
    # Given one record either side of the six-month boundary
    old = _seed_aged(181, "/velho/")
    recent = _seed_aged(179, "/recente/")

    # When the retention sweep runs
    deleted = purge_access_logs()

    # Then only the one past retention is gone
    assert deleted == 1
    assert not AccessLog.objects.filter(pk=old.pk).exists()
    assert AccessLog.objects.filter(pk=recent.pk).exists()


def test_the_purge_task_is_on_the_beat_schedule() -> None:
    # Given the Celery beat schedule
    schedule = settings.CELERY_BEAT_SCHEDULE

    # When the retention sweep is looked for
    # Then it is scheduled. A purge nobody runs is a retention policy in name only.
    assert any(
        entry["task"] == "apps.audit.tasks.purge_access_logs"
        for entry in schedule.values()
    )
    assert settings.ACCESS_LOG_RETENTION_DAYS == 180
