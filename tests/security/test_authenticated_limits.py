"""Authenticated traffic is limited per (tenant, user), which is correct HERE.

The distinction from the credential limits is the point. There the caller is not yet
identified and the tenant is attacker-chosen, so keying on it is a bypass. Here the
caller has authenticated and the tenant has been resolved from a membership they
actually hold, so the pair is a real identity and limiting against it protects the
worker pool without letting one firm's traffic starve another's.

The read limit exists for a specific reason: `TenantMiddleware` holds a transaction
open across template rendering, and workers run `sync --threads 1`, so unbounded
dashboard GETs are a denial-of-service vector rather than merely rude.
"""

from http import HTTPStatus

import pytest
from django.conf import settings
from django.test import Client, RequestFactory
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.security.ratelimit import authenticated_limit
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp, pin_rate_limit_window

pytestmark = pytest.mark.django_db(transaction=True)

TENANT_HOST = "alpha.localhost"
READS_ALLOWED = 120
WRITES_ALLOWED = 60


@pytest.fixture(autouse=True)
def _pinned_rate_limit_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep each budget inside one counting window; see the helper for why."""
    pin_rate_limit_window(monkeypatch)


@pytest.fixture(autouse=True)
def _urls(settings: SettingsWrapper) -> None:
    settings.ROOT_URLCONF = "tests.audit.urls"
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]


@pytest.fixture
def tenant() -> Tenant:
    return Tenant.objects.create(name="Alpha", slug="alpha")


def _member(tenant: Tenant, email: str) -> User:
    user = User.objects.create_user(email=email)
    Membership.objects.create(user=user, tenant=tenant, role=TenantRole.OWNER)
    enrol_totp(user)
    return user


def test_the_hundred_and_twenty_first_read_in_a_minute_is_refused(
    tenant: Tenant,
) -> None:
    # Given an authenticated member on their firm's subdomain
    client = Client()
    client.force_login(_member(tenant, "leitor@alpha.example"))

    # When they make the full read budget of requests
    statuses = {
        client.get("/ping/", headers={"host": TENANT_HOST}).status_code
        for _ in range(READS_ALLOWED)
    }

    # Then all are served, and the next one is not
    assert statuses == {HTTPStatus.OK}
    over = client.get("/ping/", headers={"host": TENANT_HOST})
    assert over.status_code == HTTPStatus.TOO_MANY_REQUESTS


def test_the_sixty_first_write_in_a_minute_is_refused(tenant: Tenant) -> None:
    # Given an authenticated member
    client = Client()
    client.force_login(_member(tenant, "escritor@alpha.example"))

    # When they make the full write budget of requests
    statuses = {
        client.post("/ping/", headers={"host": TENANT_HOST}).status_code
        for _ in range(WRITES_ALLOWED)
    }

    # Then all are served, and the next one is not. The write budget is smaller than
    # the read budget, so exhausting it must not require 120 requests.
    assert statuses == {HTTPStatus.OK}
    over = client.post("/ping/", headers={"host": TENANT_HOST})
    assert over.status_code == HTTPStatus.TOO_MANY_REQUESTS


def test_two_users_in_one_firm_do_not_share_a_bucket(tenant: Tenant) -> None:
    # Given one member who has exhausted their write budget
    first = Client()
    first.force_login(_member(tenant, "um@alpha.example"))
    for _ in range(WRITES_ALLOWED + 1):
        first.post("/ping/", headers={"host": TENANT_HOST})
    exhausted = first.post("/ping/", headers={"host": TENANT_HOST})
    assert exhausted.status_code == HTTPStatus.TOO_MANY_REQUESTS

    # When their colleague makes a request
    second = Client()
    second.force_login(_member(tenant, "dois@alpha.example"))
    colleague = second.post("/ping/", headers={"host": TENANT_HOST})

    # Then it is served: one busy user must not lock out their whole firm
    assert colleague.status_code == HTTPStatus.OK


def test_anonymous_traffic_is_not_counted_against_a_tenant_bucket() -> None:
    # Given no session
    client = Client()

    # When many reads are made
    statuses = {client.get("/ping/").status_code for _ in range(READS_ALLOWED + 5)}

    # Then none is refused by the authenticated limit, which has no identity to key
    # on. Anonymous credential traffic is governed by the per-IP limit instead.
    assert statuses == {HTTPStatus.OK}


@pytest.mark.parametrize(
    ("method", "expected_group", "expected_rate"),
    [
        ("GET", "read", "120/m"),
        ("HEAD", "read", "120/m"),
        ("POST", "write", "60/m"),
        ("PUT", "write", "60/m"),
        ("PATCH", "write", "60/m"),
        ("DELETE", "write", "60/m"),
    ],
)
def test_the_method_selects_the_right_bucket(
    method: str,
    expected_group: str,
    expected_rate: str,
    rf: RequestFactory,
) -> None:
    # Given a request using each HTTP method
    request = rf.generic(method, "/ping/")

    # When the applicable limit is resolved
    limit = authenticated_limit(request)

    # Then reads and writes land in different buckets at different rates
    assert limit.group == expected_group
    assert limit.rate == expected_rate
    assert settings.RATELIMIT_READ != settings.RATELIMIT_WRITE
