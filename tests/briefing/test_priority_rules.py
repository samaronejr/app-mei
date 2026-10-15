"""Explainable rules and stable keyset order, not a learned score."""

from datetime import UTC, date, datetime, timedelta

import pytest

from apps.briefing.queries import briefing
from apps.briefing.rules import PriorityFacts, priority_factors
from apps.briefing.types import BriefingFilters
from apps.core.tenancy import tenant_context
from tests.briefing.fixtures import briefing_setup, due_obligation

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)
TODAY = date(2026, 9, 20)


def test_every_applicable_factor_contributes_to_the_explained_sum() -> None:
    # Given independently applicable reasons, including both boundary dates.
    facts = PriorityFacts(
        statutory_due_on=TODAY + timedelta(days=3),
        request_due_on=TODAY - timedelta(days=1),
        client_visible_exception=True,
        delivery_problem=True,
        approval_requested_at=NOW - timedelta(hours=24, seconds=1),
        stale_coverage=True,
        divergent_document=True,
    )

    # When the versioned deterministic rule set explains their priority.
    factors = priority_factors(facts, now=NOW)

    # Then the stated seven rules contribute their independently specified weights.
    assert dict(factors) == {
        "statutory_due_in_days<=3": 40,
        "request_overdue": 30,
        "client_visible_exception": 20,
        "delivery_failed_or_unknown": 35,
        "approval_waiting_gt_24h": 15,
        "stale_coverage": 10,
        "divergent_document": 35,
    }
    assert sum(weight for _, weight in factors) == 185


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (PriorityFacts(), ()),
        (
            PriorityFacts(statutory_due_on=TODAY - timedelta(days=30)),
            (("statutory_due_in_days<=3", 40),),
        ),
        (
            PriorityFacts(statutory_due_on=TODAY + timedelta(days=4)),
            (),
        ),
        (PriorityFacts(request_due_on=TODAY), ()),
        (
            PriorityFacts(approval_requested_at=NOW - timedelta(hours=24)),
            (),
        ),
    ],
)
def test_boundary_facts_do_not_borrow_an_inapplicable_reason(
    facts: PriorityFacts, expected: tuple[tuple[str, int], ...]
) -> None:
    assert priority_factors(facts, now=NOW) == expected


def test_deadlines_follow_the_injected_brasilia_day_not_utc() -> None:
    # Given a UTC instant that is still the previous calendar day in Brasilia.
    moment = datetime(2026, 9, 21, 1, tzinfo=UTC)
    facts = PriorityFacts(request_due_on=TODAY)

    # Then a request due today is not reported late two hours before midnight.
    assert priority_factors(facts, now=moment) == ()


@pytest.mark.django_db(transaction=True)
def test_equal_priorities_use_a_stable_unique_key_across_pages() -> None:
    setup = briefing_setup()
    subjects = [
        due_obligation(setup, period=date(2026, month, 1)) for month in (7, 8, 9)
    ]
    with tenant_context(setup.tenant.pk):
        first = briefing(
            setup.actor,
            scope="team",
            filters=BriefingFilters(),
            limit=2,
        )
        assert first.next_cursor
        second = briefing(
            setup.actor,
            scope="team",
            filters=BriefingFilters(),
            cursor=first.next_cursor,
            limit=2,
        )
        repeated = briefing(
            setup.actor,
            scope="team",
            filters=BriefingFilters(),
            limit=2,
        )

    ordered = [item.subject_ref for item in (*first.items, *second.items)]
    assert ordered == sorted((row.pk for row in subjects), key=str)
    assert first.items == repeated.items
    assert first.total == second.total == 3
    assert second.next_cursor is None
