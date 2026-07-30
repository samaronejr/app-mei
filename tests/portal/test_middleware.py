"""A portal request runs as `app_portal`, confined to exactly one client.

Every test here uses `transaction=True`, and that is load-bearing rather than
conventional. Under a plain `django_db` the test itself owns the outer transaction, so
`PortalMiddleware`'s `atomic()` degrades to a **savepoint** — where `SET LOCAL ROLE`
works and then *merges upward* on RELEASE instead of reverting at COMMIT. Both halves of
that are wrong in the same direction: the fail-open below becomes untriggerable, and the
"no leak" assertion becomes unprovable, because no COMMIT ever happens.

The fail-open is the reason this module exists. `SET LOCAL` outside a transaction emits
a warning and does nothing (PostgreSQL 16.14). Were the role switch to land outside the
block, the request would run as `app_runtime` — for whom the RESTRICTIVE portal policies
do **not** bind, the grant being non-inheriting — with `app.client_id` set and nothing
appearing wrong. The portal user would read the firm's entire client base. So the role
is asserted **positively**, by reading `current_user` back from inside the request,
never inferred from the absence of an error.
"""

import json
from http import HTTPStatus

import pytest
from django.db import connection
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import current_client_id, current_tenant_id, tenant_context
from apps.portal.hosts import portal_slug_from_host
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

PORTAL_HOST = "acme-portal.localhost"
FIRM_HOST = "acme.localhost"
PASSWORD = "irrelevant-here"  # noqa: S105


@pytest.fixture(autouse=True)
def _portal_urls(settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]
    monkeypatch.setattr(
        "apps.portal.middleware.PORTAL_URLCONF",
        "tests.portal.urls",
    )


class Firm:
    """One firm, one of its MEI clients, a portal user and a firm-side accountant."""

    def __init__(
        self,
        tenant: Tenant,
        client: ClientCompany,
        portal_user: User,
        firm_user: User,
    ) -> None:
        self.tenant = tenant
        self.client = client
        self.portal_user = portal_user
        self.firm_user = firm_user


@pytest.fixture
def firm() -> Firm:
    tenant = Tenant.objects.create(name="Acme", slug="acme")
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding; the WITH CHECK on clients_clientcompany
        # refuses an insert with no tenant context established.
        company = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="CLIENTE ACME",
            cnpj="11222333000181",
            is_mei=True,
        )
    portal_user = User.objects.create_user(email="mei@acme.example", password=PASSWORD)
    firm_user = User.objects.create_user(email="staff@acme.example", password=PASSWORD)
    # Anyone holding an active Membership is diverted to TOTP enrolment before reaching
    # any view. Skipping this would test that redirect rather than the portal context —
    # and MFAEnforcementMiddleware runs OUTSIDE the portal transaction precisely so its
    # reads of tenants_membership and mfa_authenticator are not denied to app_portal.
    enrol_totp(portal_user)
    enrol_totp(firm_user)
    Membership.objects.create(
        user=portal_user,
        tenant=tenant,
        role=TenantRole.CLIENT_OWNER,
        client=company,
    )
    Membership.objects.create(
        user=firm_user,
        tenant=tenant,
        role=TenantRole.STAFF_ACCOUNTANT,
        client=None,
    )
    return Firm(tenant, company, portal_user, firm_user)


def _probe(client: Client, host: str) -> dict[str, str]:
    response = client.get("/probe", headers={"host": host})
    assert response.status_code == HTTPStatus.OK, response.status_code
    return dict(json.loads(response.content))


def _session_role() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        row = cursor.fetchone()
    return str(row[0]) if row else ""


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("acme-portal.example.com", "acme"),
        ("acme-portal.localhost", "acme"),
        ("acme-portal.example.com:8443", "acme"),
        ("acme.example.com", None),
        ("portal.example.com", None),
        ("-portal.example.com", None),
        ("localhost", None),
    ],
)
def test_the_portal_host_is_recognised_only_by_its_suffix(
    host: str,
    expected: str | None,
) -> None:
    # Given a hostname
    # Then the suffix is REQUIRED and STRIPPED. slug_from_host would return the literal
    # "acme-portal", which the reserved-slug validator guarantees no tenant owns, so
    # reusing it would resolve nothing and the portal would 404 for every firm.
    assert portal_slug_from_host(host) == expected


def test_an_authenticated_portal_request_runs_as_app_portal(firm: Firm) -> None:
    # Given a signed-in client-role user on the portal host
    http = Client()
    http.force_login(firm.portal_user)

    # When a view reads the database from inside the request
    seen = _probe(http, PORTAL_HOST)

    # Then the role really is app_portal and BOTH GUCs are set. Asserted positively:
    # if SET LOCAL had silently no-opped, this would read app_runtime while every other
    # symptom looked identical.
    assert seen["role"] == "app_portal"
    assert seen["tenant_guc"] == str(firm.tenant.id)
    assert seen["client_guc"] == str(firm.client.id)


def test_an_anonymous_portal_request_runs_as_app_runtime_with_empty_gucs(
    firm: Firm,
) -> None:
    # Given nobody signed in
    del firm

    # When the same view is reached
    seen = _probe(Client(), PORTAL_HOST)

    # Then the role is NOT switched and neither GUC is set. Assuming app_portal with no
    # client would make the login page itself read zero rows.
    assert seen["role"] == "app_runtime"
    assert seen["tenant_guc"] in {"", "UNSET"}
    assert seen["client_guc"] in {"", "UNSET"}


def test_a_firm_side_user_is_refused_on_the_portal_host(firm: Firm) -> None:
    # Given an accountant whose only membership is firm-side
    http = Client()
    http.force_login(firm.firm_user)

    # When they reach the portal host
    response = http.get("/probe", headers={"host": PORTAL_HOST})

    # Then they are refused. client__isnull=False is what does it: without that filter
    # their firm-side row would satisfy the check and hand them app_portal with no
    # client to confine them.
    assert response.status_code == HTTPStatus.FORBIDDEN


def test_the_admin_is_refused_before_the_transaction_opens(firm: Firm) -> None:
    # Given a signed-in portal user
    http = Client()
    http.force_login(firm.portal_user)

    # When they ask for the Django admin
    response = http.get("/admin/", headers={"host": PORTAL_HOST})

    # Then 404, not 500. AdminTenantMiddleware cannot refuse this host — its lookup
    # yields the slug "acme-portal", which no tenant owns — so this gate is the only
    # control, and reaching the admin would query tenants_tenant as app_portal and
    # raise permission denied.
    assert response.status_code == HTTPStatus.NOT_FOUND


def test_neither_the_role_nor_the_gucs_survive_the_request(firm: Firm) -> None:
    # Given a completed authenticated portal request
    http = Client()
    http.force_login(firm.portal_user)
    assert _probe(http, PORTAL_HOST)["role"] == "app_portal"

    # Then the connection is back to the firm-side role and both ContextVars are clear.
    # One connection serves many requests under sync --threads 1, so a leak here would
    # be cross-request.
    assert _session_role() == "app_runtime"
    assert current_tenant_id.get() is None
    assert current_client_id.get() is None


def test_a_second_request_on_the_same_connection_is_unaffected(firm: Firm) -> None:
    # Given a portal request followed by an anonymous one
    http = Client()
    http.force_login(firm.portal_user)
    _probe(http, PORTAL_HOST)

    # When the second request runs on the same pooled connection
    second = _probe(Client(), PORTAL_HOST)

    # Then it sees none of the first request's context
    assert second["role"] == "app_runtime"
    assert second["client_guc"] in {"", "UNSET"}


def test_a_streaming_response_is_refused(firm: Firm) -> None:
    # Given a portal view that streams
    http = Client()
    http.force_login(firm.portal_user)

    # Then it raises rather than shipping a file that downloads empty: the iterator is
    # consumed after this middleware returns, when the role and both GUCs are gone.
    with pytest.raises(TypeError, match="streaming response"):
        http.get("/streamer", headers={"host": PORTAL_HOST})


def test_a_non_atomic_view_opens_no_transaction_and_no_context(firm: Firm) -> None:
    # Given a view marked @transaction.non_atomic_requests
    http = Client()
    http.force_login(firm.portal_user)

    # When it is served on the portal host
    response = http.get("/nodb", headers={"host": PORTAL_HOST})

    # Then it succeeds without a transaction. Opening one would reintroduce exactly the
    # connection that decorator exists to avoid — and SET LOCAL ROLE inside no
    # transaction would silently no-op, which is the fail-open.
    assert response.status_code == HTTPStatus.OK
    assert _session_role() == "app_runtime"


def test_a_forged_client_id_is_ignored_from_every_direction(firm: Firm) -> None:
    # Given a sibling client the signed-in user holds no membership over
    with tenant_context(firm.tenant.id):
        # ALL_OBJECTS_OK: the client this session must never be able to name.
        sibling = ClientCompany.all_objects.create(
            tenant=firm.tenant,
            legal_name="CLIENTE BETA",
            cnpj="11444777000161",
            is_mei=True,
        )
    http = Client()
    http.force_login(firm.portal_user)

    # When the request carries that id as a query parameter, as a header, and in the
    # session all at once
    session = http.session
    session["client_id"] = str(sibling.id)
    session.save()
    response = http.get(
        f"/probe?client_id={sibling.id}",
        headers={"host": PORTAL_HOST, "x-client-id": str(sibling.id)},
    )
    seen = dict(json.loads(response.content))

    # Then app.client_id is still the membership's client. The identity is derived from
    # membership.client_id and read from nothing a caller can influence — the guard in
    # test_client_id_provenance_guard.py is what keeps it that way, but this is the
    # assertion that would notice if it stopped being true.
    assert seen["client_guc"] == str(firm.client.id)
    assert seen["client_guc"] != str(sibling.id)


def test_the_firm_host_is_untouched_by_the_portal_middleware(firm: Firm) -> None:
    # Given the firm-side accountant on the FIRM host
    http = Client()
    http.force_login(firm.firm_user)

    # When they reach a firm-side page
    response = http.get("/probe", headers={"host": FIRM_HOST})

    # Then PortalMiddleware passed the request straight through: the probe urlconf is
    # only mounted on portal hosts, so the firm host resolves against ROOT_URLCONF and
    # has no /probe. A 404 here proves the dispatcher did NOT fire.
    assert response.status_code == HTTPStatus.NOT_FOUND
