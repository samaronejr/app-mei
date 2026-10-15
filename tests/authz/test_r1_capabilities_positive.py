"""Every role the S8.1 R1 matrix opens, proven open at a gated surface.

These are the POSITIVE cases for the capability rows R1 adds: each role reaches a
real HTTP endpoint, gated exactly the way the shipping surface will be, for the
reason the matrix says it should. Two halves, mirroring the two surfaces S8.1 names:

* firm side — a client-scoped R1 view gated on `cycles.operate` under the
  `cycles_r1` flag, loading its client through `load_visible_client`, which is the
  object layer every R1 view and service is required to use (404, never 403);
* portal side — an acknowledge endpoint gated on `publications.acknowledge`, the one
  new capability that is FULL for both client roles, which is precisely why
  `core.E010` permits it to gate a portal view at all.

Both endpoints live in the TEST urlconfs (`tests/core/urls.py`,
`tests/portal/urls.py`) as stand-ins: the real R1 views land with later todos, and a
request here exercises the same middleware, the same decorators and the same
portfolio lens those views will sit behind. `transaction=True` throughout, for the
reason `tests/core/test_flags.py` documents — under a plain `django_db` the portal
middleware's `atomic()` degrades to a savepoint and `SET LOCAL ROLE` merges upward,
so role-dependent assertions would pass for the wrong reason.
"""

from http import HTTPStatus
from typing import TYPE_CHECKING, Final

import pytest
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.tenants.models import Tenant, TenantRole
from tests.ui.factories import (
    add_client,
    add_member,
    assign,
    client_base,
    make_firm,
)

if TYPE_CHECKING:  # stubs-only
    from django.test.client import _MonkeyPatchedWSGIResponse

pytestmark = pytest.mark.django_db(transaction=True)

SLUG: Final = "acme-r1"
FIRM_HOST: Final = f"{SLUG}.localhost"
PORTAL_HOST: Final = f"{SLUG}-portal.localhost"

# Reversed inside the test bodies rather than at module level: a route that does
# not exist yet must be a failing test, not a collection error.
FIRM_ROUTE: Final = "r1-client"
PORTAL_ROUTE: Final = "portal-publication-acknowledge"


class Cast:
    """One firm holding every role the S8.1 rows open, plus the rows they open on."""

    def __init__(self) -> None:
        self.firm = make_firm(SLUG)
        self.ops = add_member(
            self.firm.tenant,
            f"ops@{SLUG}.example.com",
            TenantRole.OPERATIONS_ADMIN,
        )
        self.client = add_client(
            self.firm,
            legal_name="Acme R1 Cliente MEI",
            base=client_base(0, SLUG),
        )
        self.portal_owner = add_member(
            self.firm.tenant,
            f"dono@{SLUG}.example.com",
            TenantRole.CLIENT_OWNER,
            client=self.client,
        )
        self.portal_collaborator = add_member(
            self.firm.tenant,
            f"colab@{SLUG}.example.com",
            TenantRole.CLIENT_COLLABORATOR,
            client=self.client,
        )
        # The staff accountant's portfolio is exactly this one client, which is what
        # makes the 404-for-an-invisible-client case below possible at all.
        assign(self.firm, self.client, self.firm.accountant)
        self.other_client = add_client(
            self.firm,
            legal_name="Acme R1 Invisivel MEI",
            base=client_base(1, SLUG),
        )

    @property
    def tenant(self) -> Tenant:
        """The firm every request below is scoped to."""
        return self.firm.tenant


@pytest.fixture(autouse=True)
def _urls(settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]
    settings.ROOT_URLCONF = "tests.core.urls"
    monkeypatch.setattr("apps.portal.middleware.PORTAL_URLCONF", "tests.portal.urls")


@pytest.fixture
def cast() -> Cast:
    return Cast()


def _enable_r1(tenant: Tenant) -> None:
    tenant.features = [*tenant.features, "cycles_r1"]
    tenant.save(update_fields=["features"])


def _r1_url(client: ClientCompany) -> str:
    from django.urls import reverse  # noqa: PLC0415

    return reverse(FIRM_ROUTE, args=[client.pk])


def _acknowledge(http: Client) -> "_MonkeyPatchedWSGIResponse":
    from django.urls import reverse  # noqa: PLC0415

    # The portal tree is not ROOT_URLCONF even inside this suite: the middleware
    # monkeypatch above rebinds it for REQUESTS, but reverse() still resolves
    # against the default resolver unless the urlconf is named.
    url = reverse(PORTAL_ROUTE, urlconf="tests.portal.urls")
    return http.post(url, headers={"host": PORTAL_HOST})


def _as(http_client: Client, user: User) -> Client:
    http_client.force_login(user)
    return http_client


# ------------------------------------------------------------- the firm-side gate


@pytest.mark.parametrize(
    "member",
    ["owner", "ops", "accountant"],
    ids=["owner", "operations_admin", "staff_accountant"],
)
def test_each_firm_role_reaches_the_r1_view_for_a_visible_client(
    cast: Cast,
    member: str,
) -> None:
    # Given the tenant carries cycles_r1 and the member's client is in their
    # portfolio (all of it for owner/ops, the assignment for staff)
    _enable_r1(cast.tenant)
    user = cast.ops if member == "ops" else getattr(cast.firm, member)
    http = _as(Client(), user)

    # When the gated view is asked about that client
    response = http.get(_r1_url(cast.client), headers={"host": FIRM_HOST})

    # Then the capability gate and the object layer both admit it
    assert response.status_code == HTTPStatus.OK, (
        f"{member} was refused: {response.status_code}"
    )
    assert response.json()["client"] == str(cast.client.pk)


def test_a_staff_accountant_gets_404_for_a_client_outside_their_portfolio(
    cast: Cast,
) -> None:
    # Given the same gated view and a client the accountant was never assigned
    _enable_r1(cast.tenant)
    http = _as(Client(), cast.firm.accountant)

    # When it is asked about the invisible client
    response = http.get(_r1_url(cast.other_client), headers={"host": FIRM_HOST})

    # Then the answer is 404, never 403: `load_visible_client` must not confirm the
    # id names a real row, which is the fact the portfolio boundary exists to hold
    assert response.status_code == HTTPStatus.NOT_FOUND, (
        f"answered {response.status_code}; a 403 would confirm this client exists"
    )


def test_the_r1_view_is_absent_while_the_tenant_flag_is_off(cast: Cast) -> None:
    # Given NO flag on the tenant — R1 surfaces ship dark
    http = _as(Client(), cast.firm.owner)

    # Then even a FULL holder is answered 404, not 403: an unflagged tenant must not
    # learn that the surface exists
    response = http.get(_r1_url(cast.client), headers={"host": FIRM_HOST})
    assert response.status_code == HTTPStatus.NOT_FOUND


def test_a_client_role_cannot_reach_the_firm_side_r1_view(cast: Cast) -> None:
    # Given a portal identity signed in and pointed at the FIRM host
    _enable_r1(cast.tenant)
    http = _as(Client(), cast.portal_owner)

    # Then TenantMiddleware refuses it firm context before any gate runs: a portal
    # membership is proof of nothing on the firm's own surface
    response = http.get(_r1_url(cast.client), headers={"host": FIRM_HOST})
    assert response.status_code == HTTPStatus.FORBIDDEN


# ------------------------------------------------------------ the portal-side gate


@pytest.mark.parametrize(
    "member",
    ["portal_owner", "portal_collaborator"],
    ids=["client_owner", "client_collaborator"],
)
def test_both_client_roles_reach_the_portal_acknowledge_endpoint(
    cast: Cast,
    member: str,
) -> None:
    # Given the flag is on and a portal identity confined to the firm's client
    _enable_r1(cast.tenant)
    http = _as(Client(), getattr(cast, member))

    # When the acknowledge endpoint is POSTed on the portal host
    response = _acknowledge(http)

    # Then the stash-resolved capability is FULL for both client roles — the exact
    # property core.E010 demands of any capability gating a portal view
    assert response.status_code == HTTPStatus.OK, (
        f"{member} was refused: {response.status_code}"
    )
    assert response.json() == {"acknowledged": True}


def test_the_acknowledge_endpoint_is_absent_while_the_flag_is_off(
    cast: Cast,
) -> None:
    # Given a portal identity but no flag on the tenant
    http = _as(Client(), cast.portal_owner)

    # Then the stash-answered flag check 404s the surface
    response = _acknowledge(http)
    assert response.status_code == HTTPStatus.NOT_FOUND


def test_a_firm_member_cannot_reach_the_portal_acknowledge_endpoint(
    cast: Cast,
) -> None:
    # Given a firm-side account pointed at the PORTAL host
    _enable_r1(cast.tenant)
    http = _as(Client(), cast.firm.owner)

    # Then PortalMiddleware refuses before the gate runs: a firm membership names no
    # client, so there is nothing to confine the request to
    response = _acknowledge(http)
    assert response.status_code == HTTPStatus.FORBIDDEN


def test_an_anonymous_portal_request_is_refused(cast: Cast) -> None:
    # Given nobody signed in at all
    _enable_r1(cast.tenant)
    response = _acknowledge(Client())

    # Then the gate denies rather than the middleware granting anonymous context
    assert response.status_code == HTTPStatus.FORBIDDEN
