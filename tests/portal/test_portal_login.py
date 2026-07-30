"""A client logs in at their portal host and sees exactly their own company.

This is the tracer bullet: the first request that exercises the whole stack end to end —
host dispatch, the role switch, both GUCs, the RESTRICTIVE policies, and a rendered
template. What it proves is not the view's logic, which is deliberately trivial. It
proves that a page written with **no client filter of its own** still shows one client's
data, because the database refused the rest.

The obligation count is the falsifiable half. The sibling client is seeded with a
*different* number on purpose: a count assertion against a sibling with the same number
would pass with the policy dropped, and "the other company never appears" is not
falsifiable at all when the view fetches a single company by primary key.
"""

import datetime as dt
from http import HTTPStatus

import pytest
from django.conf import settings
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.obligations.models import Obligation, ObligationType
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

PORTAL_HOST = "acme-portal.localhost"
PASSWORD = "irrelevant-here"  # noqa: S105
ALPHA_OBLIGATIONS = 2
BETA_OBLIGATIONS = 5


@pytest.fixture(autouse=True)
def _portal_hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]


class Firm:
    """One firm, two MEI clients with DIFFERENT obligation counts, one portal user."""

    def __init__(
        self,
        tenant: Tenant,
        alpha: ClientCompany,
        beta: ClientCompany,
        owner: User,
        staff: User,
    ) -> None:
        self.tenant = tenant
        self.alpha = alpha
        self.beta = beta
        self.owner = owner
        self.staff = staff


@pytest.fixture
def firm() -> Firm:
    tenant = Tenant.objects.create(name="Acme", slug="acme")
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding under an established tenant context.
        alpha = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="CLIENTE ALPHA",
            cnpj="11222333000181",
            is_mei=True,
        )
        # ALL_OBJECTS_OK: the sibling whose rows must never be counted.
        beta = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="CLIENTE BETA",
            cnpj="11444777000161",
            is_mei=True,
        )
        obligation_type = ObligationType.objects.first()
        assert obligation_type is not None
        for client, count in ((alpha, ALPHA_OBLIGATIONS), (beta, BETA_OBLIGATIONS)):
            for month in range(count):
                Obligation.objects.create(
                    tenant=tenant,
                    client=client,
                    obligation_type=obligation_type,
                    competence_month=dt.date(2026, month + 1, 1),
                    nominal_due_date=dt.date(2026, month + 2, 20),
                    resolved_due_date=dt.date(2026, month + 2, 20),
                )
    owner = User.objects.create_user(email="alpha@mei.example", password=PASSWORD)
    staff = User.objects.create_user(email="staff@acme.example", password=PASSWORD)
    enrol_totp(owner)
    enrol_totp(staff)
    Membership.objects.create(
        user=owner,
        tenant=tenant,
        role=TenantRole.CLIENT_OWNER,
        client=alpha,
    )
    Membership.objects.create(
        user=staff,
        tenant=tenant,
        role=TenantRole.STAFF_ACCOUNTANT,
        client=None,
    )
    return Firm(tenant, alpha, beta, owner, staff)


def test_the_portal_landing_shows_the_signed_in_client(firm: Firm) -> None:
    # Given a signed-in client-role user
    http = Client()
    http.force_login(firm.owner)

    # When they load their portal home
    response = http.get("/", headers={"host": PORTAL_HOST})
    body = response.content.decode()

    # Then their own company is rendered
    assert response.status_code == HTTPStatus.OK
    assert firm.alpha.legal_name in body


def test_the_sibling_client_is_never_rendered(firm: Firm) -> None:
    # Given the same session
    http = Client()
    http.force_login(firm.owner)

    # When the page renders
    body = http.get("/", headers={"host": PORTAL_HOST}).content.decode()

    # Then the other client of the same firm does not appear anywhere in it
    assert firm.beta.legal_name not in body


def test_the_obligation_count_is_only_this_client_s(firm: Firm) -> None:
    # Given a sibling seeded with a DIFFERENT number of obligations
    http = Client()
    http.force_login(firm.owner)

    # When the count is rendered
    body = http.get("/", headers={"host": PORTAL_HOST}).content.decode()

    # Then it is alpha's alone, though the view applies NO client filter — the
    # RESTRICTIVE policy did the confining. Were the policy dropped this would read
    # the sum, which is why the two counts differ.
    assert f"<dd>{ALPHA_OBLIGATIONS}</dd>" in body
    assert f"<dd>{ALPHA_OBLIGATIONS + BETA_OBLIGATIONS}</dd>" not in body


def test_a_firm_side_user_authenticates_but_cannot_then_use_the_portal(
    firm: Firm,
) -> None:
    # Given a firm-side accountant posting valid credentials at the portal host.
    # The login POST is ANONYMOUS at middleware entry, so the membership check does not
    # run and allauth authenticates against the platform-global accounts_user: the
    # credentials ARE accepted and a session IS issued.
    http = Client()
    http.force_login(firm.staff)

    # When the now-authenticated user makes their next request
    response = http.get("/", headers={"host": PORTAL_HOST})

    # Then THAT is where they are refused. Asserting "login fails" would fail against
    # correct code; the refusal is one hop later than the wording suggests.
    assert response.status_code == HTTPStatus.FORBIDDEN


def test_cookies_stay_host_only() -> None:
    # Then session cookies are not shared with sibling hosts. A domain cookie here
    # would let one firm's host set a cookie honoured on another's portal.
    assert settings.SESSION_COOKIE_DOMAIN is None
    assert settings.CSRF_COOKIE_DOMAIN is None
