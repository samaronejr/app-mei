"""Who may open the revenue-threshold queue, and what they see once inside.

The queue is a COLLECTION view: it lists every client at or past the warning band, so
there is no single object for a refined grant level to be evaluated against. It was
gated
on `reports.view_financial`, which is `Limited` for `operations_admin`, and
`require_can` passes no object -- so `_is_attached_to(user, None)` returned False and
the
view was an unconditional 403 for that role. `visible_nav_items` draws only `full`, so
the link never rendered either, and nobody clicks a 403 they cannot see.

It now gates on `obligations.view_revenue_threshold_queue`, a firm-side collection
capability. Two things this file is careful to keep apart:

* **Authorization** is the capability. It decides 200 or 403.
* **Portfolio scoping** is `visible_clients()`. It decides which rows appear.

Scoping is not a substitute for the gate -- a role with no capability must be refused
outright, not handed an empty page -- so every refusal below is asserted against a queue
that demonstrably has rows for someone else.
"""

from datetime import date
from decimal import Decimal
from http import HTTPStatus

import pytest
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.authz.matrix import MATRIX
from apps.authz.services import can, resolve_level
from apps.clients.models import ClientCompany
from apps.core.navigation import visible_nav_items
from apps.core.tenancy import client_context, tenant_context
from apps.obligations.models import MonthlyRevenue
from apps.tenants.models import TenantRole
from tests.ui.factories import (
    Firm,
    add_client,
    add_member,
    assign,
    client_base,
    make_firm,
)

pytestmark = pytest.mark.django_db(transaction=True)

QUEUE = "queue-threshold"
CAPABILITY = "obligations.view_revenue_threshold_queue"
OLD_CAPABILITY = "reports.view_financial"
NAV_LABEL = "Limite"


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


def _over_the_warning_band(firm: Firm, company: ClientCompany) -> None:
    """Push one client past 80% of its ceiling so it appears in the queue."""
    with tenant_context(firm.tenant.id):
        MonthlyRevenue.objects.create(
            tenant=firm.tenant,
            client=company,
            competence_month=date(date.today().year, 1, 1),  # noqa: DTZ011
            gross_amount=Decimal("75000.00"),
        )


@pytest.fixture
def alpha() -> Firm:
    firm = make_firm("alpha", client_count=2)
    for company in firm.clients:
        _over_the_warning_band(firm, company)
    return firm


def _ops(firm: Firm) -> User:
    return add_member(
        firm.tenant,
        f"ops@{firm.tenant.slug}.example.com",
        TenantRole.OPERATIONS_ADMIN,
    )


def _accountant(firm: Firm) -> User:
    return add_member(
        firm.tenant,
        f"acc2@{firm.tenant.slug}.example.com",
        TenantRole.STAFF_ACCOUNTANT,
    )


# ------------------------------------------------------------------ the published
# matrix


def test_the_matrix_carries_exactly_the_six_approved_values() -> None:
    # Given the published capability
    row = next(entry for entry in MATRIX if entry.slug == CAPABILITY)

    # Then it is a firm-side collection capability: every firm role FULL, both client
    # roles NONE. Any other shape is a different decision than the one approved.
    assert str(row.label) == "View revenue threshold queue"
    assert [str(level) for level in row.levels] == [
        "full",
        "full",
        "full",
        "full",
        "none",
        "none",
    ]


def test_the_previous_capability_is_untouched() -> None:
    # Given reports.view_financial, which this fix deliberately did NOT broaden
    row = next(entry for entry in MATRIX if entry.slug == OLD_CAPABILITY)

    # Then it still carries its original values, so no other gate using it moved
    assert [str(level) for level in row.levels] == [
        "full",
        "full",
        "full",
        "limited",
        "full",
        "limited",
    ]


# --------------------------------------------------------------------------- the gate


def test_an_operations_admin_reaches_the_queue(alpha: Firm) -> None:
    # Given the role the old gate refused unconditionally
    session = alpha.sign_in(_ops(alpha))

    # When it opens the queue
    response = session.get(reverse(QUEUE))

    # Then it is admitted. Against the old reports.view_financial gate this is a 403.
    assert response.status_code == HTTPStatus.OK


def test_a_staff_accountant_reaches_the_queue(alpha: Firm) -> None:
    # Given a staff accountant with one client assigned
    accountant = _accountant(alpha)
    assign(alpha, alpha.clients[0], accountant)

    # When it opens the queue
    response = alpha.sign_in(accountant).get(reverse(QUEUE))

    # Then it is admitted
    assert response.status_code == HTTPStatus.OK


@pytest.mark.parametrize(
    "role",
    [TenantRole.CLIENT_OWNER, TenantRole.CLIENT_COLLABORATOR],
)
def test_a_client_role_is_refused_the_capability(alpha: Firm, role: str) -> None:
    """Asserted at `can()`, not over HTTP, and the distinction is deliberate.

    A client-role membership carries a client, so `TenantMiddleware`'s firm-side lookup
    (`client=None`) never matches it and the request is turned away before any
    capability
    is consulted. An HTTP 403 here would therefore pass without the gate existing at all
    -- it would be measuring tenant routing. The capability is what this asserts.
    """
    user = add_member(
        alpha.tenant,
        f"{role}@{alpha.tenant.slug}.example.com",
        role,
        client=alpha.clients[0],
    )
    with tenant_context(alpha.tenant.id), client_context(alpha.clients[0].id):
        assert can(user, CAPABILITY) is False
        assert str(resolve_level(user, CAPABILITY)) == "none"


def test_the_refusal_is_not_an_empty_queue(alpha: Firm) -> None:
    # Given the queue has rows for somebody
    body = alpha.as_owner().get(reverse(QUEUE)).content.decode()

    # Then a 403 above means refused, not "nothing to show" -- which is the disguise a
    # permissions bug in a list view wears
    assert alpha.clients[0].legal_name in body


# ------------------------------------------------------------------------- navigation


def test_an_operations_admin_sees_the_link(alpha: Firm) -> None:
    # Given the role whose link never rendered: visible_nav_items draws only FULL.
    # Inside a tenant context: role_of reads the context var, so outside one every level
    # resolves NONE and the whole bar is empty -- which would pass this test's negative
    # counterpart for no reason at all.
    user = _ops(alpha)
    with tenant_context(alpha.tenant.id):
        labels = [str(item.label) for item in visible_nav_items(user)]

    # Then the destination it may now reach is offered
    assert NAV_LABEL in labels


def test_a_staff_accountant_sees_the_link(alpha: Firm) -> None:
    user = _accountant(alpha)
    with tenant_context(alpha.tenant.id):
        labels = [str(item.label) for item in visible_nav_items(user)]
    assert NAV_LABEL in labels


def test_a_client_role_does_not_see_the_link(alpha: Firm) -> None:
    # Given a client-side role
    user = add_member(
        alpha.tenant,
        f"portal-nav@{alpha.tenant.slug}.example.com",
        TenantRole.CLIENT_OWNER,
        client=alpha.clients[0],
    )

    # Then no link is offered to a page it would be refused -- and the bar is NOT simply
    # empty, which is what makes the absence mean something
    with tenant_context(alpha.tenant.id), client_context(alpha.clients[0].id):
        labels = [str(item.label) for item in visible_nav_items(user)]
    assert labels, (
        "an empty bar would pass this assertion without the capability existing"
    )
    assert NAV_LABEL not in labels


# ------------------------------------------------------- scoping, layered under the
# gate


def test_an_accountant_sees_only_assigned_clients(alpha: Firm) -> None:
    # Given two clients over the band and only one assigned
    accountant = _accountant(alpha)
    assign(alpha, alpha.clients[0], accountant)

    # When the queue is opened
    body = alpha.sign_in(accountant).get(reverse(QUEUE)).content.decode()

    # Then the gate admitted them and the SCOPING narrowed them -- two layers, not one
    assert alpha.clients[0].legal_name in body
    assert alpha.clients[1].legal_name not in body


def test_an_operations_admin_sees_the_whole_firm(alpha: Firm) -> None:
    # Given operations_admin is a FIRM_WIDE_ROLE in portfolio_scope
    body = alpha.sign_in(_ops(alpha)).get(reverse(QUEUE)).content.decode()

    # Then both clients appear, which is the scope its role carries by definition
    assert alpha.clients[0].legal_name in body
    assert alpha.clients[1].legal_name in body


def test_another_firms_clients_never_appear(alpha: Firm) -> None:
    # Given a second firm with its own client over the band
    beta = make_firm("beta", client_count=1)
    _over_the_warning_band(beta, beta.clients[0])

    # When alpha's operations admin opens the queue
    body = alpha.sign_in(_ops(alpha)).get(reverse(QUEUE)).content.decode()

    # Then tenant isolation holds beneath both the gate and the scoping
    assert beta.clients[0].legal_name not in body


def test_an_unassigned_client_is_absent_for_an_accountant(alpha: Firm) -> None:
    # Given a client over the band that nobody assigned to this accountant
    accountant = _accountant(alpha)
    assign(alpha, alpha.clients[0], accountant)
    stranger = add_client(
        alpha,
        legal_name="CLIENTE NAO ATRIBUIDO",
        base=client_base(9, "alpha"),
    )
    _over_the_warning_band(alpha, stranger)

    # When the queue is opened
    body = alpha.sign_in(accountant).get(reverse(QUEUE)).content.decode()

    # Then it is absent
    assert stranger.legal_name not in body


# ------------------------------------------------- the capability answers, role by role


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (TenantRole.OWNER, True),
        (TenantRole.STAFF_ACCOUNTANT, True),
        (TenantRole.OPERATIONS_ADMIN, True),
    ],
)
def test_can_answers_true_for_every_firm_role(
    alpha: Firm,
    role: str,
    expected: bool,
) -> None:
    user = add_member(alpha.tenant, f"{role}-cap@{alpha.tenant.slug}.example.com", role)
    with tenant_context(alpha.tenant.id):
        assert can(user, CAPABILITY) is expected
        assert str(resolve_level(user, CAPABILITY)) == "full"
