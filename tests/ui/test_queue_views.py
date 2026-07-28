"""The four queue screens: who may open them, what they return, and in what shape.

The authorization cases are the reason this file is written test-first. A permission
bug in a list view has a characteristic disguise — the page still returns 200 and
simply shows nothing — and "no rows" is indistinguishable from "no work to do". Every
refusal here is asserted as a 403 with a non-empty queue behind it, so a mutation that
turns the guard into a silent filter cannot pass.
"""

from datetime import date, timedelta
from decimal import Decimal
from http import HTTPStatus

import pytest
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import OnboardingItem, OnboardingStatus
from apps.core.tenancy import tenant_context
from apps.obligations.models import (
    MonthlyRevenue,
    Obligation,
    ObligationStatus,
    ObligationType,
)
from apps.tenants.models import Membership, TenantRole
from tests.ui.factories import Firm, add_member, assign, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

QUEUE_URLS = (
    "queue-due-soon",
    "queue-overdue",
    "queue-onboarding",
    "queue-threshold",
)

# The three queues an operations admin is refused by the published matrix:
# das.generate is ❌ for that role, and reports.view_financial is "Limited",
# which can() answers False for with no object in hand.
REFUSED_FOR_OPERATIONS = ("queue-due-soon", "queue-overdue", "queue-threshold")

PAGE_SIZE = 25


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


def _stock(firm: Firm, client_count: int) -> None:
    das = ObligationType.objects.get(code="DAS")
    today = date.today()  # noqa: DTZ011
    for company in firm.clients[:client_count]:
        with tenant_context(firm.tenant.id):
            Obligation.objects.create(
                tenant=firm.tenant,
                client=company,
                obligation_type=das,
                competence_month=today.replace(day=1),
                nominal_due_date=today + timedelta(days=2),
                resolved_due_date=today + timedelta(days=2),
                status=ObligationStatus.SCHEDULED,
            )
            Obligation.objects.create(
                tenant=firm.tenant,
                client=company,
                obligation_type=das,
                competence_month=(today.replace(day=1) - timedelta(days=1)).replace(
                    day=1
                ),
                nominal_due_date=today - timedelta(days=9),
                resolved_due_date=today - timedelta(days=9),
                status=ObligationStatus.SCHEDULED,
            )
            MonthlyRevenue.objects.create(
                tenant=firm.tenant,
                client=company,
                competence_month=date(today.year, 1, 1),
                gross_amount=Decimal("75000.00"),
            )
            item = OnboardingItem.objects.filter(client=company).first()
            assert item is not None
            item.status = OnboardingStatus.BLOCKED
            item.blocked_reason = "aguardando procuração"
            item.save(update_fields=["status", "blocked_reason"])


@pytest.fixture
def alpha() -> Firm:
    firm = make_firm("alpha-view", client_count=3)
    assign(firm, firm.clients[0], firm.accountant)
    _stock(firm, 3)
    return firm


def _operations_admin(firm: Firm) -> User:
    return add_member(
        firm.tenant,
        f"ops@{firm.tenant.slug}.example.com",
        TenantRole.OPERATIONS_ADMIN,
    )


# --------------------------------------------------------------------- authorization


@pytest.mark.parametrize("name", REFUSED_FOR_OPERATIONS)
def test_a_refused_queue_answers_403_and_not_an_empty_page(
    alpha: Firm,
    name: str,
) -> None:
    """The whole point. A 200 with zero rows would hide the permissions bug."""
    session = alpha.sign_in(_operations_admin(alpha))
    response = session.get(reverse(name))
    assert response.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.parametrize("name", REFUSED_FOR_OPERATIONS)
def test_the_refused_queue_is_not_merely_empty_for_everyone(
    alpha: Firm,
    name: str,
) -> None:
    """Without this, the 403 above could be passing on a queue that has no rows."""
    body = alpha.as_owner().get(reverse(name)).content.decode()
    assert alpha.clients[0].legal_name in body, (
        f"{name} is empty for the owner too, so the 403 above proves nothing"
    )


@pytest.mark.parametrize("name", QUEUE_URLS)
def test_an_anonymous_visitor_is_sent_to_sign_in(alpha: Firm, name: str) -> None:
    from django.test import Client  # noqa: PLC0415

    response = Client(SERVER_NAME=alpha.host).get(reverse(name))
    assert response.status_code == HTTPStatus.FOUND
    assert reverse("account_login") in response.headers["Location"]


@pytest.mark.parametrize("name", QUEUE_URLS)
def test_every_queue_opens_for_the_owner(alpha: Firm, name: str) -> None:
    assert alpha.as_owner().get(reverse(name)).status_code == HTTPStatus.OK


# --------------------------------------------------------------------------- scoping


def test_the_owner_and_the_accountant_see_different_rows(alpha: Firm) -> None:
    owner_body = alpha.as_owner().get(reverse("queue-due-soon")).content.decode()
    staff_body = alpha.as_accountant().get(reverse("queue-due-soon")).content.decode()

    assigned = alpha.clients[0].legal_name
    unassigned = alpha.clients[1].legal_name

    assert assigned in owner_body
    assert unassigned in owner_body
    assert assigned in staff_body
    assert unassigned not in staff_body


def test_another_firms_rows_never_reach_a_queue(alpha: Firm) -> None:
    beta = make_firm("beta-view", client_count=2)
    _stock(beta, 2)
    body = alpha.as_owner().get(reverse("queue-due-soon")).content.decode()
    for company in beta.clients:
        assert company.legal_name not in body


# ---------------------------------------------------------------------------- shapes


@pytest.mark.parametrize("name", QUEUE_URLS)
def test_a_full_request_returns_a_whole_page(alpha: Firm, name: str) -> None:
    body = alpha.as_owner().get(reverse(name)).content.decode()
    assert body.lstrip().startswith("<!DOCTYPE html>")
    assert "<main" in body


@pytest.mark.parametrize("name", QUEUE_URLS)
def test_an_htmx_request_returns_only_the_fragment(alpha: Firm, name: str) -> None:
    response = alpha.as_owner().get(reverse(name), headers={"hx-request": "true"})
    body = response.content.decode()
    assert response.status_code == HTTPStatus.OK
    assert "<!DOCTYPE html>" not in body
    assert "<main" not in body
    assert 'id="fila"' in body


def test_the_fragment_carries_the_same_rows_as_the_page(alpha: Firm) -> None:
    session = alpha.as_owner()
    page = session.get(reverse("queue-due-soon")).content.decode()
    fragment = session.get(
        reverse("queue-due-soon"),
        headers={"hx-request": "true"},
    ).content.decode()
    for company in alpha.clients:
        assert (company.legal_name in page) == (company.legal_name in fragment)


def test_no_queue_response_is_streamed(alpha: Firm) -> None:
    """TenantMiddleware raises on a streaming response, because its iterator would be
    consumed after the tenant transaction closed and every lazy query would return
    zero rows — a page that downloads perfectly and is empty."""
    session = alpha.as_owner()
    for name in QUEUE_URLS:
        response = session.get(reverse(name))
        assert not response.streaming


# ------------------------------------------------------------------------ pagination


def test_pagination_is_stable_and_does_not_repeat_or_drop_rows(alpha: Firm) -> None:
    firm = make_firm("paged-view", client_count=0)
    from tests.ui.factories import add_client, client_base  # noqa: PLC0415

    for index in range(PAGE_SIZE + 7):
        add_client(
            firm,
            legal_name=f"Paged Cliente {index:03d} MEI",
            base=client_base(index, firm.tenant.slug),
        )
    _stock(firm, PAGE_SIZE + 7)

    session = firm.as_owner()

    def names_on(**params: str) -> set[str]:
        body = session.get(reverse("queue-due-soon"), params).content.decode()
        return {c.legal_name for c in firm.clients if c.legal_name in body}

    on_first = names_on()
    on_second = names_on(pagina="2")

    assert len(on_first) == PAGE_SIZE
    assert on_first & on_second == set(), "a row appears on two pages"
    assert on_first | on_second == {c.legal_name for c in firm.clients}
    # Re-requested after page two: the same page must hold the same rows, or a row
    # slips between pages and is never worked.
    assert names_on() == on_first, "the same request returned different rows"


def test_every_queue_ordering_is_total(alpha: Firm) -> None:
    """Stability is a property of the ORDER BY, not of what the planner happens to do.

    The behavioural test above cannot see this on its own: when every row shares a due
    date the comparison is fully tied, and PostgreSQL is free to return any order at
    all — it simply tends to return the same one twice in a row. Only a *total* order
    guarantees a row cannot slip between pages, and an order is total exactly when it
    ends in a unique column.
    """
    from apps.obligations.queries import (  # noqa: PLC0415
        due_soon,
        onboarding_blocked,
        overdue,
    )

    with tenant_context(alpha.tenant.id):
        for queue in (due_soon, overdue, onboarding_blocked):
            ordering = queue(alpha.owner).query.order_by
            assert ordering[-1] == "pk", (
                f"{queue.__name__} orders by {ordering}, which is not a total order"
            )


def test_an_out_of_range_page_does_not_explode(alpha: Firm) -> None:
    response = alpha.as_owner().get(reverse("queue-due-soon"), {"pagina": 999})
    assert response.status_code == HTTPStatus.OK


def test_a_nonsense_page_number_does_not_explode(alpha: Firm) -> None:
    response = alpha.as_owner().get(reverse("queue-due-soon"), {"pagina": "abc"})
    assert response.status_code == HTTPStatus.OK


# --------------------------------------------------------------------------- filters


def test_filtering_by_assignee_narrows_the_queue(alpha: Firm) -> None:
    session = alpha.as_owner()
    filtered = session.get(
        reverse("queue-due-soon"),
        {"responsavel": str(alpha.accountant.pk)},
    ).content.decode()

    assert alpha.clients[0].legal_name in filtered
    assert alpha.clients[1].legal_name not in filtered


def test_an_assignee_from_another_firm_is_ignored_rather_than_honoured(
    alpha: Firm,
) -> None:
    """Honouring it would make the filter an existence oracle for other firms' users."""
    beta = make_firm("beta-filter", client_count=1)
    response = alpha.as_owner().get(
        reverse("queue-due-soon"),
        {"responsavel": str(beta.owner.pk)},
    )
    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert alpha.clients[1].legal_name in body, "the filter silently emptied the queue"


def test_filtering_by_status_narrows_the_queue(alpha: Firm) -> None:
    with tenant_context(alpha.tenant.id):
        Obligation.objects.filter(client=alpha.clients[0]).update(
            status=ObligationStatus.DUE,
        )
    body = (
        alpha.as_owner()
        .get(
            reverse("queue-due-soon"),
            {"situacao": ObligationStatus.DUE},
        )
        .content.decode()
    )

    assert alpha.clients[0].legal_name in body
    assert alpha.clients[1].legal_name not in body


def test_an_unknown_status_is_ignored_rather_than_emptying_the_queue(
    alpha: Firm,
) -> None:
    body = (
        alpha.as_owner()
        .get(
            reverse("queue-due-soon"),
            {"situacao": "inexistente"},
        )
        .content.decode()
    )
    assert alpha.clients[0].legal_name in body


# ---------------------------------------------------------------------------- counts


def test_the_counts_fragment_refreshes_without_a_full_page(alpha: Firm) -> None:
    response = alpha.as_owner().get(
        reverse("queue-counts"),
        headers={"hx-request": "true"},
    )
    body = response.content.decode()
    assert response.status_code == HTTPStatus.OK
    assert "<!DOCTYPE html>" not in body
    assert 'id="contagens"' in body


def test_the_counts_match_the_queues_they_summarise(alpha: Firm) -> None:
    from apps.obligations.queries import due_soon, overdue  # noqa: PLC0415

    with tenant_context(alpha.tenant.id):
        expected_due = due_soon(alpha.owner).count()
        expected_overdue = overdue(alpha.owner).count()

    body = alpha.as_owner().get(reverse("queue-counts")).content.decode()
    assert f'data-count-due-soon="{expected_due}"' in body
    assert f'data-count-overdue="{expected_overdue}"' in body


def test_the_counts_omit_a_queue_the_account_may_not_open(alpha: Firm) -> None:
    """Reporting "14 atrasadas" to someone refused that queue leaks the number."""
    session = alpha.sign_in(_operations_admin(alpha))
    body = session.get(reverse("queue-counts")).content.decode()
    assert "data-count-overdue=" not in body
    assert "data-count-onboarding-blocked=" in body, "the fragment is not simply empty"


def test_a_membership_revoked_between_requests_stops_working(alpha: Firm) -> None:
    session = alpha.as_owner()
    assert session.get(reverse("queue-due-soon")).status_code == HTTPStatus.OK
    Membership.objects.filter(user=alpha.owner, tenant=alpha.tenant).update(
        is_active=False,
    )
    assert session.get(reverse("queue-due-soon")).status_code == HTTPStatus.FORBIDDEN
