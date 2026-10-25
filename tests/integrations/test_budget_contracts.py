"""The zero tariff does not remove the independent Focus journal."""

import pytest

from apps.integrations.budget_pricing import BudgetDeniedError, microbrl, price
from apps.integrations.call_contracts import Operation
from apps.integrations.call_models import ProviderCall
from tests.integrations.test_call_protocol import SyntheticPolicy


def test_plan_included_focus_read_still_requires_journal() -> None:
    # Given a Focus document retrieval explicitly included in the plan.
    operation = Operation(
        "FC-NFE",
        "focus",
        "v1",
        "read",
        "document_capture",
        "incluido_no_plano",
        SyntheticPolicy(),
    )
    # When the protocol decides whether to reserve and journal.
    # Then zero cost cannot turn it into an untracked transmission.
    assert operation.journaled


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "NaN",
        "Infinity",
        "-0.24",
        "0.0000001",
        "1e400",
        "9223372036854.775808",
    ],
)
def test_malformed_prices_never_become_zero(value: str | None) -> None:
    # Given an absent, non-finite, negative or sub-micro-BRL configuration value.
    # When it crosses the money configuration boundary.
    with pytest.raises(BudgetDeniedError):
        microbrl(value)
    # Then admission gets a typed refusal, never a zero-price fallback.


@pytest.mark.parametrize(
    ("value", "expected"), [("0.24", 240_000), ("0", 0), ("0.000001", 1)]
)
def test_prices_convert_exactly_without_floats(value: str, expected: int) -> None:
    # Given decimal BRL values, including an explicit zero tariff.
    # When converted at the configuration boundary.
    assert microbrl(value) == expected
    # Then no rounding can silently increase or reduce the reserved amount.


@pytest.mark.parametrize("billing_class", ["emissao_lote", "", "incluido_no_plano"])
def test_unmapped_serpro_billing_class_never_reserves_zero(billing_class: str) -> None:
    # Given a SERPRO call whose billing class has no unit-price metric.
    call = ProviderCall(provider="serpro", billing_class=billing_class)
    # When it is priced under a recorded tariff revision.
    with pytest.raises(BudgetDeniedError) as refused:
        price(call, "synthetic-tariff-v1")
    # Then an unmapped class is an absent price, never a free reservation.
    assert refused.value.reason == "tarifa_ausente"
