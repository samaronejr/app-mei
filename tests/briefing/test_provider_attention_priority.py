"""Provider attention participates in the explained queue priority."""

from datetime import UTC, datetime

import pytest

from apps.briefing.rules import PriorityFacts, priority_factors


@pytest.mark.parametrize("attention", [False, True])
@pytest.mark.parametrize("overdue", [False, True])
def test_provider_attention_adds_its_weight_without_changing_other_reasons(
    attention: bool, overdue: bool
) -> None:
    now = datetime(2026, 10, 21, 15, tzinfo=UTC)
    facts = PriorityFacts(
        provider_check_attention=attention,
        statutory_due_on=now.date() if overdue else None,
    )
    expected = []
    if overdue:
        expected.append(("statutory_due_in_days<=3", 40))
    if attention:
        expected.append(("provider_check_attention", 25))
    assert priority_factors(facts, now=now) == tuple(expected)
