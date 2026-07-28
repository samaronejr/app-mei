"""Who is forced into MFA enrolment, and who is deliberately not.

Two populations are gated, and the second is the one that is easy to forget: a
platform operator may hold `is_staff` and **no** `Membership` at all, which would
leave the highest-privilege surface in the product — the admin console — as the only
unprotected one. Gating on membership alone reads as complete and is not.
"""

from http import HTTPStatus

import pytest
from allauth.account.models import EmailAddress
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.mfa import MFA_ENROLMENT_URL_NAME, requires_mfa
from apps.accounts.models import User
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "correct-horse-battery-staple"  # noqa: S105


@pytest.fixture(autouse=True)
def _urls(settings: SettingsWrapper) -> None:
    settings.ROOT_URLCONF = "tests.accounts.urls"
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]


@pytest.fixture
def tenant() -> Tenant:
    return Tenant.objects.create(name="Alpha Contabilidade", slug="alpha")


def _make_user(email: str, *, is_staff: bool = False) -> User:
    return User.objects.create_user(email=email, password=PASSWORD, is_staff=is_staff)


def _enrolment_url() -> str:
    return reverse(MFA_ENROLMENT_URL_NAME)


def _signed_in_client(user: User) -> Client:
    """Sign in through the real login form rather than by forcing the session.

    `force_login` writes the session key directly, so allauth never records *when*
    authentication happened and treats every subsequent sensitive action as stale —
    which sends enrolment through a reauthentication step that has nothing to do with
    the gate under test.
    """
    EmailAddress.objects.create(
        user=user,
        email=user.email,
        verified=True,
        primary=True,
    )
    client = Client()
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": PASSWORD},
    )
    assert response.status_code == HTTPStatus.FOUND, "login did not authenticate"
    return client


def test_a_firm_user_without_mfa_is_redirected_from_a_tenant_url(
    tenant: Tenant,
) -> None:
    # Given a firm-side user holding an active membership and no authenticator
    user = _make_user("owner@alpha.example")
    Membership.objects.create(user=user, tenant=tenant, role=TenantRole.OWNER)
    client = Client()
    client.force_login(user)

    # When a tenant screen is requested
    response = client.get("/painel/", headers={"host": "alpha.localhost"})

    # Then the request never reaches the view
    assert response.status_code == HTTPStatus.FOUND
    assert response.headers["Location"].startswith(_enrolment_url())


def test_a_firm_user_with_mfa_reaches_the_view(tenant: Tenant) -> None:
    # Given the same user, now carrying a TOTP authenticator
    user = _make_user("owner@alpha.example")
    Membership.objects.create(user=user, tenant=tenant, role=TenantRole.OWNER)
    enrol_totp(user)
    client = Client()
    client.force_login(user)

    # When the same tenant screen is requested
    response = client.get("/painel/", headers={"host": "alpha.localhost"})

    # Then it renders. Without this the redirect assertion above would be satisfied by
    # a gate that redirects everyone unconditionally.
    assert response.status_code == HTTPStatus.OK
    assert response.content == b"painel"


def test_a_staff_operator_with_no_membership_is_gated_too() -> None:
    # Given a platform operator: is_staff, and deliberately NO membership anywhere
    user = _make_user("ops@platform.example", is_staff=True)
    assert not Membership.objects.filter(user=user).exists()
    client = Client()
    client.force_login(user)

    # When the admin console is requested
    response = client.get("/painel/")

    # Then the gate still fires. Keying only on Membership would leave exactly this
    # account — the most privileged one — unprotected.
    assert response.status_code == HTTPStatus.FOUND
    assert response.headers["Location"].startswith(_enrolment_url())


def test_a_user_with_neither_membership_nor_staff_is_exempt() -> None:
    # Given a client-side user: no membership, not staff. Exempt in v1 by policy.
    user = _make_user("mei@cliente.example")
    client = Client()
    client.force_login(user)

    # When a screen is requested
    response = client.get("/painel/")

    # Then it renders unchallenged
    assert response.status_code == HTTPStatus.OK


def test_an_inactive_membership_does_not_trigger_the_gate(tenant: Tenant) -> None:
    # Given a former employee whose membership was revoked
    user = _make_user("ex@alpha.example")
    Membership.objects.create(
        user=user,
        tenant=tenant,
        role=TenantRole.STAFF_ACCOUNTANT,
        is_active=False,
    )

    # When the policy is evaluated
    # Then they are not firm-side any more
    assert requires_mfa(user) is False


def test_the_enrolment_url_itself_is_not_gated(tenant: Tenant) -> None:
    # Given a gated user who signed in through the real login form, so allauth has a
    # recent-authentication record and does not interpose its reauthentication step
    user = _make_user("owner@alpha.example")
    Membership.objects.create(user=user, tenant=tenant, role=TenantRole.OWNER)
    client = _signed_in_client(user)

    # When the enrolment page it was redirected to is followed
    response = client.get(_enrolment_url())

    # Then it is served rather than redirected again. A gate that also guards its own
    # destination is an infinite loop, not a control.
    assert response.status_code == HTTPStatus.OK


def test_the_liveness_probe_is_not_gated(tenant: Tenant) -> None:
    # Given a gated user
    user = _make_user("owner@alpha.example")
    Membership.objects.create(user=user, tenant=tenant, role=TenantRole.OWNER)
    client = Client()
    client.force_login(user)

    # When the liveness probe is requested
    response = client.get("/healthz")

    # Then it answers. An orchestrator must not restart a healthy process because the
    # operator who happens to be logged in has not enrolled.
    assert response.status_code == HTTPStatus.OK


def test_anonymous_requests_are_not_gated() -> None:
    # Given no session at all
    client = Client()

    # When a screen is requested
    response = client.get("/painel/")

    # Then the gate stays out of the way; authentication is a different control
    assert response.status_code == HTTPStatus.OK


def test_the_policy_helper_reports_both_populations(tenant: Tenant) -> None:
    # Given one user per population
    member = _make_user("member@alpha.example")
    Membership.objects.create(user=member, tenant=tenant, role=TenantRole.OWNER)
    operator = _make_user("operator@platform.example", is_staff=True)
    outsider = _make_user("outsider@cliente.example")

    # When the policy is evaluated for each
    # Then membership and staff are independently sufficient
    assert requires_mfa(member) is True
    assert requires_mfa(operator) is True
    assert requires_mfa(outsider) is False
