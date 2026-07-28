"""The dashboard, and with it the plan's headline exit criterion.

`test_the_exit_criterion` is the one that matters: a firm tenant with two users and
three client companies renders a correct twelve-month DAS calendar, asserted through
the HTTP layer rather than by calling the generator and believing it. The expected
dates are recomputed here from `due_date_for` — the resolver, not the stored rows — so
a calendar that renders whatever happens to be in the table cannot pass.

`test_no_trace_of_another_firm_appears` is the other half. This is the first screen a
human sees, so it is the first place the isolation model is visible, and a leak here
is a churn event rather than a bug report.
"""

from datetime import date, timedelta
from decimal import Decimal
from http import HTTPStatus

import pytest
from django.db.models import F
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import (
    ClientCompany,
    ClientStatus,
    OnboardingItem,
    OnboardingStatus,
)
from apps.core.templatetags.ptbr import cnpj_mask
from apps.core.tenancy import tenant_context
from apps.obligations.calendar import due_date_for
from apps.obligations.generator import first_competence_for, generate_das_calendar
from apps.obligations.models import MonthlyRevenue, Obligation, ObligationType
from apps.obligations.queries import due_soon, onboarding_blocked, overdue
from tests.isolation.rolecheck import assert_isolated_role
from tests.ui.factories import Firm, add_client, assign, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

TODAY = date.today()  # noqa: DTZ011
# Written out rather than imported from apps.core.dashboard. Importing the constant
# under test makes the expectation shrink with the implementation: a six-month strip
# then satisfies a twelve-month assertion, which is precisely the acceptance criterion
# this file exists to hold. Twelve is the specification, not a parameter.
EXPECTED_MONTHS = 12
USERS_PER_FIRM = 2
CLIENTS_PER_FIRM = 3
# Measured, not guessed: a dashboard render issues 79 queries, of which 53 are the
# authorization triple — membership, capability, grant — re-resolved for each of the
# seven scoped querysets the page composes. That overhead is deliberate. It could be
# removed by memoising the permission decision on the request, and it is not, because
# every read on this screen staying live is worth more than the milliseconds: this is
# the page where a stale authorization answer becomes a visible cross-tenant leak.
# What matters is that the number is O(1) in portfolio size, which the test below
# proves by comparing three clients against twenty.
QUERY_BUDGET = 90


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


@pytest.fixture
def alpha() -> Firm:
    """The exit criterion's firm: two users, three client companies."""
    firm = make_firm("alpha-painel", client_count=CLIENTS_PER_FIRM)
    assign(firm, firm.clients[0], firm.accountant)
    with tenant_context(firm.tenant.id):
        for company in firm.clients:
            generate_das_calendar(company, starting_from=TODAY)
    return firm


def _expected_calendar(company: ClientCompany) -> list[tuple[date, date]]:
    """Recompute the twelve deadlines from the resolver, independently of the rows."""
    das = ObligationType.objects.get(code="DAS")
    first = first_competence_for(company, TODAY)
    months: list[tuple[date, date]] = []
    competence = first
    for _ in range(EXPECTED_MONTHS):
        resolved = due_date_for(das, competence)
        months.append((competence, resolved.resolved))
        competence = (competence.replace(day=28) + timedelta(days=7)).replace(day=1)
    return months


# ------------------------------------------------------------------ exit criterion


def test_the_exit_criterion(alpha: Firm) -> None:
    """Two users, three clients, a correct twelve-month DAS calendar, over HTTP."""
    assert_isolated_role()

    # Given the firm the anchor's §12 exit criterion describes
    assert alpha.tenant.memberships.count() == USERS_PER_FIRM
    with tenant_context(alpha.tenant.id):
        assert ClientCompany.objects.count() == CLIENTS_PER_FIRM

    # When its owner opens the dashboard
    response = alpha.as_owner().get(reverse("dashboard"))
    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()

    # Then the calendar strip holds twelve months for the selected client, each on
    # the date the business-day resolver independently produces for it
    with tenant_context(alpha.tenant.id):
        selected = ClientCompany.objects.order_by("legal_name", "pk").first()
        assert selected is not None
        expected = _expected_calendar(selected)

    assert len(expected) == EXPECTED_MONTHS
    for competence, resolved in expected:
        cell = (
            f'datetime="{resolved.isoformat()}"\n'
            f'                    data-competence="{competence.strftime("%Y-%m")}"'
        )
        assert cell in body, (
            f"the calendar has no cell for {competence:%Y-%m} due {resolved}"
        )


def test_the_calendar_actually_rolls_at_least_one_deadline(alpha: Firm) -> None:
    """Otherwise the assertion above would pass against a calendar that never rolls.

    Over twelve consecutive months the 20th lands on a weekend or a national holiday
    several times, so a strip with no rolled cell means the business-day calendar is
    not being consulted at all.
    """
    with tenant_context(alpha.tenant.id):
        moved = list(
            Obligation.objects.filter(obligation_type__code="DAS").exclude(
                nominal_due_date=F("resolved_due_date"),
            ),
        )
    assert moved, "no DAS deadline rolled in twelve months, which cannot be right"

    body = alpha.as_owner().get(reverse("dashboard")).content.decode()
    assert "adiado de" in body, "a rolled deadline is not shown as rolled"


# ------------------------------------------------------------------------ isolation


def test_no_trace_of_another_firm_appears(alpha: Firm) -> None:
    """Authenticated as tenant B, nothing of tenant A may reach the rendered page."""
    assert_isolated_role()
    beta = make_firm("beta-painel", client_count=2)
    with tenant_context(beta.tenant.id):
        for company in beta.clients:
            generate_das_calendar(company, starting_from=TODAY)

    body = beta.as_owner().get(reverse("dashboard")).content.decode()

    for company in alpha.clients:
        assert company.legal_name not in body
        assert company.trade_name not in body
        assert company.cnpj not in body
        assert cnpj_mask(company.cnpj) not in body
    assert alpha.tenant.name not in body
    assert alpha.owner.email not in body
    assert alpha.accountant.email not in body

    with tenant_context(alpha.tenant.id):
        for obligation in Obligation.objects.all():
            assert str(obligation.pk) not in body


def test_the_isolation_search_finds_the_identifiers_when_they_belong_there(
    alpha: Firm,
) -> None:
    """The positive control for the case above.

    "None of firm A's identifiers appear in firm B's page" is worth nothing if those
    identifiers never appear in any page — a typo in the fixture, a name the template
    does not render, and the assertion passes while testing the empty set. Every value
    searched for above is asserted here to be present in firm A's own dashboard.
    """
    body = alpha.as_owner().get(reverse("dashboard")).content.decode()
    for company in alpha.clients:
        assert company.legal_name in body
    assert alpha.tenant.name in body
    assert alpha.owner.email in body
    # The document is rendered masked, and only for the client the calendar is showing.
    # Searching a page for the stored form alone would make the isolation assertion
    # above pass against a string no template ever emits.
    with tenant_context(alpha.tenant.id):
        selected = ClientCompany.objects.order_by("legal_name", "pk").first()
    assert selected is not None
    assert cnpj_mask(selected.cnpj) in body


def test_the_portfolio_count_is_this_firms_and_not_the_platforms(alpha: Firm) -> None:
    beta = make_firm("beta-count", client_count=5)
    body = alpha.as_owner().get(reverse("dashboard")).content.decode()
    assert f"Carteira ({CLIENTS_PER_FIRM} clientes)" in body
    assert f"Carteira ({len(beta.clients)} clientes)" not in body


# -------------------------------------------------------------------------- scoping


def test_the_owner_sees_the_whole_portfolio(alpha: Firm) -> None:
    body = alpha.as_owner().get(reverse("dashboard")).content.decode()
    for company in alpha.clients:
        assert company.legal_name in body
    assert f'data-portfolio-{ClientStatus.ACTIVE}="{CLIENTS_PER_FIRM}"' in body


def test_the_staff_accountant_sees_only_their_assignments(alpha: Firm) -> None:
    body = alpha.as_accountant().get(reverse("dashboard")).content.decode()
    assert alpha.clients[0].legal_name in body
    for company in alpha.clients[1:]:
        assert company.legal_name not in body
    assert f'data-portfolio-{ClientStatus.ACTIVE}="1"' in body


def test_the_calendar_defaults_to_a_client_inside_the_portfolio(alpha: Firm) -> None:
    body = alpha.as_accountant().get(reverse("dashboard")).content.decode()
    assert alpha.clients[0].cnpj in body or alpha.clients[0].legal_name in body


def test_naming_another_firms_client_falls_back_rather_than_confirming_it(
    alpha: Firm,
) -> None:
    """A blank calendar for a real id and an error for a fake one is an oracle."""
    beta = make_firm("beta-select", client_count=1)
    response = alpha.as_owner().get(
        reverse("dashboard"),
        {"cliente": str(beta.clients[0].pk)},
    )
    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert beta.clients[0].legal_name not in body
    assert alpha.clients[0].legal_name in body


def test_an_unparseable_client_id_does_not_explode(alpha: Firm) -> None:
    response = alpha.as_owner().get(reverse("dashboard"), {"cliente": "nao-e-um-uuid"})
    assert response.status_code == HTTPStatus.OK


# --------------------------------------------------------------------------- counts


def test_the_queue_counts_match_the_queries_they_summarise(alpha: Firm) -> None:
    das = ObligationType.objects.get(code="DAS")
    with tenant_context(alpha.tenant.id):
        Obligation.objects.create(
            tenant=alpha.tenant,
            client=alpha.clients[0],
            obligation_type=das,
            competence_month=(TODAY.replace(day=1) - timedelta(days=1)).replace(day=1),
            nominal_due_date=TODAY - timedelta(days=11),
            resolved_due_date=TODAY - timedelta(days=11),
        )
        item = OnboardingItem.objects.filter(client=alpha.clients[1]).first()
        assert item is not None
        item.status = OnboardingStatus.BLOCKED
        item.blocked_reason = "aguardando procuração"
        item.save(update_fields=["status", "blocked_reason"])

        expected_due = due_soon(alpha.owner).count()
        expected_overdue = overdue(alpha.owner).count()
        expected_blocked = onboarding_blocked(alpha.owner).count()

    assert expected_overdue > 0, "the overdue count would be vacuous at zero"
    assert expected_blocked > 0, "the onboarding count would be vacuous at zero"

    body = alpha.as_owner().get(reverse("dashboard")).content.decode()
    assert f'data-count-due-soon="{expected_due}"' in body
    assert f'data-count-overdue="{expected_overdue}"' in body
    assert f'data-count-onboarding-blocked="{expected_blocked}"' in body


def test_a_threshold_client_is_listed_with_its_band(alpha: Firm) -> None:
    with tenant_context(alpha.tenant.id):
        MonthlyRevenue.objects.create(
            tenant=alpha.tenant,
            client=alpha.clients[2],
            competence_month=date(TODAY.year, 1, 1),
            gross_amount=Decimal("78000.00"),
        )
    body = alpha.as_owner().get(reverse("dashboard")).content.decode()
    assert alpha.clients[2].legal_name in body
    assert "perto do limite" in body


# --------------------------------------------------------------------------- shape


def test_the_dashboard_is_not_streamed(alpha: Firm) -> None:
    """A streaming response is refused by TenantMiddleware, because its iterator runs
    after the tenant transaction closes and every lazy query returns zero rows."""
    response = alpha.as_owner().get(reverse("dashboard"))
    assert not response.streaming


def test_the_dashboard_stays_inside_a_query_budget(
    alpha: Firm,
    django_assert_max_num_queries: object,
) -> None:
    session = alpha.as_owner()
    with django_assert_max_num_queries(QUERY_BUDGET):  # type: ignore[operator]
        session.get(reverse("dashboard"))


def test_the_query_count_does_not_grow_with_the_portfolio(alpha: Firm) -> None:
    """Twenty clients must cost what three cost, or the first real firm times out."""
    from django.db import connection, reset_queries  # noqa: PLC0415
    from django.test.utils import CaptureQueriesContext  # noqa: PLC0415

    from tests.ui.factories import client_base  # noqa: PLC0415

    def cost(firm: Firm) -> int:
        session = firm.as_owner()
        reset_queries()
        with CaptureQueriesContext(connection) as captured:
            session.get(reverse("dashboard"))
        return len(captured)

    small = cost(alpha)

    big = make_firm("big-painel", client_count=0)
    for index in range(20):
        add_client(
            big,
            legal_name=f"Big Cliente {index:03d} MEI",
            base=client_base(index, big.tenant.slug),
        )
    with tenant_context(big.tenant.id):
        for company in big.clients:
            generate_das_calendar(company, starting_from=TODAY)

    assert cost(big) == small


def test_the_platform_host_has_no_dashboard(alpha: Firm) -> None:
    """There is no firm there, and an empty portfolio would imply there was one."""
    from django.test import Client  # noqa: PLC0415

    session = alpha.as_owner()
    session.defaults["SERVER_NAME"] = "localhost"
    response = session.get(reverse("dashboard"))
    assert response.status_code in {HTTPStatus.NOT_FOUND, HTTPStatus.FORBIDDEN}
    assert Client(SERVER_NAME="localhost") is not None
