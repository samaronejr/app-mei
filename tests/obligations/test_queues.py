"""The four work queues: what they contain, whose they are, and what they cost.

The cost tests are not performance hygiene. A queue that issues one query per client
is the difference between a dashboard that opens and one that times out at the exact
moment a firm's portfolio becomes worth having, so the budgets here are asserted with
fifty clients seeded and compared against the same call with three.

The scoping tests are the other half. A staff accountant's queue must contain their
book of business and nothing else, and the empty-state cases exist because "no rows"
is the answer this product must never give by accident.
"""

from collections.abc import Callable, Iterable
from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.clients.models import (
    ClientCompany,
    ClientStatus,
    OnboardingItem,
    OnboardingStatus,
)
from apps.core.tenancy import tenant_context
from apps.obligations.models import (
    MonthlyRevenue,
    Obligation,
    ObligationStatus,
    ObligationType,
)
from apps.obligations.queries import (
    DUE_SOON_HORIZON_DAYS,
    due_soon,
    onboarding_blocked,
    overdue,
    threshold_warning,
)
from apps.obligations.threshold import ThresholdBand
from tests.isolation.rolecheck import assert_isolated_role
from tests.ui.factories import Firm, assign, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

# The four queues share no return type — three querysets over different models and
# one tuple — so the shape a parametrised test can rely on is exactly this: called
# with an actor, iterable afterwards.
Queue = Callable[..., Iterable[object]]

TODAY = date(2026, 6, 15)
YEAR = 2026
BULK_CLIENTS = 50


def _obligation(
    firm: Firm,
    client: ClientCompany,
    *,
    due: date,
    status: str = ObligationStatus.SCHEDULED,
    competence: date | None = None,
) -> Obligation:
    with tenant_context(firm.tenant.id):
        return Obligation.objects.create(
            tenant=firm.tenant,
            client=client,
            obligation_type=ObligationType.objects.get(code="DAS"),
            competence_month=competence or date(due.year, due.month, 1),
            nominal_due_date=due,
            resolved_due_date=due,
            status=status,
        )


def _bill(firm: Firm, client: ClientCompany, amount: Decimal) -> None:
    with tenant_context(firm.tenant.id):
        MonthlyRevenue.objects.create(
            tenant=firm.tenant,
            client=client,
            competence_month=date(YEAR, 1, 1),
            gross_amount=amount,
        )


@pytest.fixture
def alpha() -> Firm:
    """One firm, three clients, and the staff accountant assigned to exactly one."""
    firm = make_firm("alpha-queue", client_count=3)
    assign(firm, firm.clients[0], firm.accountant)
    return firm


@pytest.fixture
def beta() -> Firm:
    firm = make_firm("beta-queue", client_count=1)
    assign(firm, firm.clients[0], firm.accountant)
    return firm


def _names(rows: Iterable[Obligation]) -> set[str]:
    return {row.client.legal_name for row in rows}


# --------------------------------------------------------------------------- content


def test_due_soon_holds_the_next_seven_days_and_nothing_further(alpha: Firm) -> None:
    assert_isolated_role()
    inside = _obligation(alpha, alpha.clients[0], due=TODAY + timedelta(days=6))
    today_itself = _obligation(alpha, alpha.clients[1], due=TODAY)
    _obligation(alpha, alpha.clients[2], due=TODAY + timedelta(days=30))

    with tenant_context(alpha.tenant.id):
        rows = list(due_soon(alpha.owner, today=TODAY))

    assert {row.pk for row in rows} == {inside.pk, today_itself.pk}


def test_due_soon_boundary_is_inclusive_of_the_seventh_day(alpha: Firm) -> None:
    edge = _obligation(
        alpha,
        alpha.clients[0],
        due=TODAY + timedelta(days=DUE_SOON_HORIZON_DAYS),
    )
    beyond = _obligation(
        alpha,
        alpha.clients[1],
        due=TODAY + timedelta(days=DUE_SOON_HORIZON_DAYS + 1),
    )

    with tenant_context(alpha.tenant.id):
        found = {row.pk for row in due_soon(alpha.owner, today=TODAY)}

    assert edge.pk in found
    assert beyond.pk not in found


@pytest.mark.parametrize(
    "settled",
    [ObligationStatus.PAID, ObligationStatus.WAIVED],
)
def test_due_soon_omits_what_is_already_settled(alpha: Firm, settled: str) -> None:
    _obligation(
        alpha,
        alpha.clients[0],
        due=TODAY + timedelta(days=2),
        status=settled,
    )
    with tenant_context(alpha.tenant.id):
        assert list(due_soon(alpha.owner, today=TODAY)) == []


def test_overdue_holds_what_is_past_due_and_unsettled(alpha: Firm) -> None:
    late = _obligation(alpha, alpha.clients[0], due=TODAY - timedelta(days=1))
    _obligation(alpha, alpha.clients[1], due=TODAY)
    _obligation(
        alpha,
        alpha.clients[2],
        due=TODAY - timedelta(days=40),
        status=ObligationStatus.PAID,
    )

    with tenant_context(alpha.tenant.id):
        rows = list(overdue(alpha.owner, today=TODAY))

    assert {row.pk for row in rows} == {late.pk}


def test_overdue_does_not_wait_for_a_sweep_to_relabel_the_row(alpha: Firm) -> None:
    """A queue that trusts the status column reports nothing when the sweep is down.

    Silence is this product's worst failure mode, so overdue is decided by the date
    the obligation resolved to, not by whether a background job has caught up.
    """
    stale = _obligation(
        alpha,
        alpha.clients[0],
        due=TODAY - timedelta(days=5),
        status=ObligationStatus.SCHEDULED,
    )
    with tenant_context(alpha.tenant.id):
        assert {row.pk for row in overdue(alpha.owner, today=TODAY)} == {stale.pk}


def test_onboarding_blocked_holds_only_blocked_checklist_items(alpha: Firm) -> None:
    with tenant_context(alpha.tenant.id):
        item = OnboardingItem.objects.filter(client=alpha.clients[0]).first()
        assert item is not None, "the checklist is seeded on client creation"
        item.status = OnboardingStatus.BLOCKED
        item.blocked_reason = "aguardando procuração no e-CAC"
        item.save(update_fields=["status", "blocked_reason"])

        rows = list(onboarding_blocked(alpha.owner))

    assert [row.pk for row in rows] == [item.pk]
    assert all(row.status == OnboardingStatus.BLOCKED for row in rows)


def test_onboarding_blocked_is_empty_when_nothing_is_blocked(alpha: Firm) -> None:
    with tenant_context(alpha.tenant.id):
        rows = onboarding_blocked(alpha.owner)
        assert list(rows) == []


@pytest.mark.parametrize(
    ("billed", "expected"),
    [
        # 80% of the 81.000,00 ceiling is 64.800,00.
        (Decimal("60000.00"), False),
        (Decimal("65000.00"), True),
        (Decimal("82000.00"), True),
        (Decimal("120000.00"), True),
    ],
)
def test_threshold_warning_holds_the_warning_and_exceeded_bands(
    alpha: Firm,
    billed: Decimal,
    expected: bool,
) -> None:
    """80% of the 2026 ceiling is the warning line; everything above stays listed."""
    _bill(alpha, alpha.clients[0], billed)

    with tenant_context(alpha.tenant.id):
        rows = list(threshold_warning(alpha.owner, year=YEAR))

    listed = {row.client.pk for row in rows}
    assert (alpha.clients[0].pk in listed) is expected


def test_threshold_warning_reports_the_band_it_matched_on(alpha: Firm) -> None:
    """An accountant needs to know which of the four outcomes applies, not just that
    one does — the two exceeded bands are a year apart in consequence."""
    _bill(alpha, alpha.clients[0], Decimal("120000.00"))

    with tenant_context(alpha.tenant.id):
        row = next(iter(threshold_warning(alpha.owner, year=YEAR)))

    assert row.status.band is ThresholdBand.EXCEEDED_OVER_TOLERANCE
    assert row.status.desenquadramento_effective_on == date(YEAR, 1, 1)


# ----------------------------------------------------------------------- empty states


@pytest.mark.parametrize(
    "queue",
    [due_soon, overdue, onboarding_blocked, threshold_warning],
)
def test_an_empty_queue_is_an_empty_result_never_none(
    alpha: Firm, queue: Queue
) -> None:
    with tenant_context(alpha.tenant.id):
        result = queue(alpha.owner)
    assert result is not None
    assert list(result) == []


# ---------------------------------------------------------------------------- scoping


def test_an_owner_sees_the_whole_portfolio(alpha: Firm) -> None:
    for client in alpha.clients:
        _obligation(alpha, client, due=TODAY + timedelta(days=1))

    with tenant_context(alpha.tenant.id):
        rows = list(due_soon(alpha.owner, today=TODAY))

    assert len(rows) == len(alpha.clients)


def test_a_staff_accountant_sees_only_the_clients_assigned_to_them(
    alpha: Firm,
) -> None:
    for client in alpha.clients:
        _obligation(alpha, client, due=TODAY + timedelta(days=1))

    with tenant_context(alpha.tenant.id):
        rows = list(due_soon(alpha.accountant, today=TODAY))

    assert _names(rows) == {alpha.clients[0].legal_name}


def test_an_unassigned_staff_accountant_gets_nothing_rather_than_everything(
    alpha: Firm,
) -> None:
    """The permissive failure would be a staff accountant with an empty book seeing
    the entire firm, which is the direction that leaks."""
    with tenant_context(alpha.tenant.id):
        ClientCompany.objects.filter(pk=alpha.clients[0].pk).update(
            status=ClientStatus.ACTIVE,
        )
    for client in alpha.clients:
        _obligation(alpha, client, due=TODAY + timedelta(days=1))

    from apps.clients.models import ClientAssignment  # noqa: PLC0415

    with tenant_context(alpha.tenant.id):
        ClientAssignment.objects.filter(user=alpha.accountant).delete()
        rows = list(due_soon(alpha.accountant, today=TODAY))

    assert rows == []


@pytest.mark.parametrize(
    "queue",
    [due_soon, overdue, onboarding_blocked, threshold_warning],
)
def test_no_queue_ever_returns_another_firms_rows(
    alpha: Firm,
    beta: Firm,
    queue: Queue,
) -> None:
    assert_isolated_role()
    _obligation(beta, beta.clients[0], due=TODAY + timedelta(days=1))
    _obligation(
        beta,
        beta.clients[0],
        due=TODAY - timedelta(days=1),
        competence=date(2026, 5, 1),
    )
    _bill(beta, beta.clients[0], Decimal("120000.00"))
    with tenant_context(beta.tenant.id):
        item = OnboardingItem.objects.filter(client=beta.clients[0]).first()
        assert item is not None
        item.status = OnboardingStatus.BLOCKED
        item.blocked_reason = "bloqueado"
        item.save(update_fields=["status", "blocked_reason"])

    with tenant_context(alpha.tenant.id):
        rows = list(queue(alpha.owner))

    assert rows == []


def test_a_users_own_firm_is_not_reachable_from_another_firms_context(
    alpha: Firm,
    beta: Firm,
) -> None:
    """Asking as beta's owner while alpha's context is established yields nothing."""
    _obligation(beta, beta.clients[0], due=TODAY + timedelta(days=1))

    with tenant_context(alpha.tenant.id):
        rows = list(due_soon(beta.owner, today=TODAY))

    assert rows == []
