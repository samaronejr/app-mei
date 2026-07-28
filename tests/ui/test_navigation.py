"""The navigation bar offers only what the account may actually reach.

Hiding a link is not the control — `require_can` on the destination is, and the last
case here proves it by asking for the hidden screen directly and getting a 403. What
the nav prevents is an interface that offers a firm owner's screens to a staff
accountant and then refuses them, which teaches people to distrust every other button.
"""

from http import HTTPStatus

import pytest
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.core.navigation import NAV, visible_nav_items
from apps.core.tenancy import tenant_context
from tests.ui.factories import Firm, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

OWNER_ONLY = "Equipe"
SHARED = "Exportar clientes"
# Two reads for the whole table: the capability existence check and the grant lookup,
# plus the membership read that resolves the role.
NAV_QUERY_BUDGET = 3


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


@pytest.fixture
def firm() -> Firm:
    return make_firm("alpha-nav")


def _labels(firm: Firm, user: object) -> list[str]:
    with tenant_context(firm.tenant.id):
        return [str(item.label) for item in visible_nav_items(user)]  # type: ignore[arg-type]


def test_every_shipped_destination_resolves() -> None:
    """A nav entry pointing at a url name that does not exist is a dead link.

    Asserted unconditionally rather than by skipping unresolvable entries, because
    skipping is how an item silently disappears for everyone.
    """
    for item in NAV:
        assert item.url().startswith("/"), item.url_name


def test_an_owner_sees_the_owner_only_item(firm: Firm) -> None:
    assert OWNER_ONLY in _labels(firm, firm.owner)


def test_a_staff_accountant_does_not_see_the_owner_only_item(firm: Firm) -> None:
    """The published matrix gives `users.create` to the owner and not to staff."""
    assert OWNER_ONLY not in _labels(firm, firm.accountant)


def test_a_staff_accountants_nav_is_not_merely_empty(firm: Firm) -> None:
    """Otherwise the case above would pass for a nav that shows nobody anything."""
    labels = _labels(firm, firm.accountant)
    assert labels != []
    assert SHARED in labels


def test_an_anonymous_visitor_gets_no_nav(firm: Firm) -> None:
    client = Client(SERVER_NAME=firm.host)
    response = client.get(reverse("dsr-submit"))
    assert response.status_code == HTTPStatus.OK
    assert "<nav" not in response.content.decode()


def test_resolving_the_whole_bar_costs_a_fixed_number_of_queries(
    firm: Firm,
    django_assert_num_queries: object,
) -> None:
    """One query per item would make the bar the most expensive thing on the page."""
    with tenant_context(firm.tenant.id), django_assert_num_queries(NAV_QUERY_BUDGET):  # type: ignore[operator]
        visible_nav_items(firm.owner)


def test_the_owners_rendered_page_offers_the_owner_only_link(firm: Firm) -> None:
    body = firm.as_owner().get(reverse("team")).content.decode()
    assert reverse("team") in body
    assert OWNER_ONLY in body


def test_the_staff_accountants_rendered_page_omits_it(firm: Firm) -> None:
    body = firm.as_accountant().get(reverse("dsr-submit")).content.decode()
    assert SHARED in body, "the shared item should still be offered"
    assert f'href="{reverse("team")}"' not in body


def test_the_hidden_screen_refuses_rather_than_rendering_empty(firm: Firm) -> None:
    """Hiding is cosmetic; this is the control. A 200 with no rows would hide a bug."""
    response = firm.as_accountant().get(reverse("team"))
    assert response.status_code == HTTPStatus.FORBIDDEN
