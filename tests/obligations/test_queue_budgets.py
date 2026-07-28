"""Every queue must cost the same with fifty clients as with three.

This is the T-045 failure QA, and it is written as a comparison rather than as a
single budget on purpose. A fixed number asserted once can be satisfied by a query
that is slow for a different reason; two portfolios of very different sizes producing
the *identical* count is what actually rules out an N+1.
"""

from collections.abc import Callable, Iterable
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import connection, reset_queries
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import User
from apps.clients.models import OnboardingItem, OnboardingStatus
from apps.core.tenancy import tenant_context
from apps.obligations.models import (
    MonthlyRevenue,
    Obligation,
    ObligationStatus,
    ObligationType,
)
from apps.obligations.queries import (
    due_soon,
    onboarding_blocked,
    overdue,
    threshold_warning,
)
from tests.ui.factories import Firm, add_client, client_base, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

# Anchored on the real current date so every queue is called with its default
# reference — which is the way a view calls it, and the way an N+1 would appear.
TODAY = timezone.localdate()
YEAR = TODAY.year
SMALL = 3
LARGE = 50

Queue = Callable[..., Iterable[Any]]
QUEUES: tuple[tuple[str, Queue], ...] = (
    ("due_soon", due_soon),
    ("overdue", overdue),
    ("onboarding_blocked", onboarding_blocked),
    ("threshold_warning", threshold_warning),
)


def _populate(firm: Firm, count: int) -> None:
    """Give every client one upcoming obligation, one late one, and revenue."""
    das = ObligationType.objects.get(code="DAS")
    for index in range(count):
        company = add_client(
            firm,
            legal_name=f"{firm.tenant.slug} Cliente {index} MEI",
            base=client_base(index),
        )
        with tenant_context(firm.tenant.id):
            Obligation.objects.create(
                tenant=firm.tenant,
                client=company,
                obligation_type=das,
                competence_month=TODAY.replace(day=1),
                nominal_due_date=TODAY + timedelta(days=3),
                resolved_due_date=TODAY + timedelta(days=3),
                status=ObligationStatus.SCHEDULED,
            )
            Obligation.objects.create(
                tenant=firm.tenant,
                client=company,
                obligation_type=das,
                competence_month=(TODAY.replace(day=1) - timedelta(days=1)).replace(
                    day=1
                ),
                nominal_due_date=TODAY - timedelta(days=10),
                resolved_due_date=TODAY - timedelta(days=10),
                status=ObligationStatus.SCHEDULED,
            )
            MonthlyRevenue.objects.create(
                tenant=firm.tenant,
                client=company,
                competence_month=date(YEAR, 1, 1),
                gross_amount=Decimal("70000.00"),
            )
            blocked = OnboardingItem.objects.filter(client=company).first()
            assert blocked is not None, "the checklist templates are seeded"
            blocked.status = OnboardingStatus.BLOCKED
            blocked.blocked_reason = "aguardando procuração"
            blocked.save(update_fields=["status", "blocked_reason"])


def _cost(queue: Queue, user: User, firm: Firm) -> int:
    """Count the queries a queue issues, including evaluating every row it returns."""
    with tenant_context(firm.tenant.id):
        reset_queries()
        with CaptureQueriesContext(connection) as captured:
            rows = list(queue(user))
            for row in rows:
                # Touching the related objects a template would render is the point:
                # a missing select_related shows up here and nowhere else.
                _ = getattr(row, "client", None) and row.client.legal_name
        return len(captured)


@pytest.fixture
def small() -> Firm:
    firm = make_firm("small-budget")
    _populate(firm, SMALL)
    return firm


@pytest.fixture
def large() -> Firm:
    firm = make_firm("large-budget")
    _populate(firm, LARGE)
    return firm


@pytest.mark.parametrize(("name", "queue"), QUEUES)
def test_the_query_count_does_not_grow_with_the_portfolio(
    small: Firm,
    large: Firm,
    name: str,
    queue: Queue,
) -> None:
    with tenant_context(large.tenant.id):
        assert len(list(queue(large.owner))) > SMALL, (
            f"{name} returned too few rows for the comparison to mean anything"
        )

    small_cost = _cost(queue, small.owner, small)
    large_cost = _cost(queue, large.owner, large)
    print(  # noqa: T201
        f"  {name}: {SMALL} clients -> {small_cost} queries | "
        f"{LARGE} clients -> {large_cost} queries",
    )
    assert small_cost == large_cost, name


@pytest.mark.parametrize(("name", "queue"), QUEUES)
def test_each_queue_stays_inside_a_fixed_budget(
    large: Firm,
    name: str,
    queue: Queue,
    django_assert_max_num_queries: object,
) -> None:
    """A ceiling as well as a comparison: identical-but-huge passes the test above."""
    budget = 20
    with django_assert_max_num_queries(budget):  # type: ignore[operator]
        _cost(queue, large.owner, large)
