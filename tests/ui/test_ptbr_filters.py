"""pt-BR presentation of money, dates and competence months."""

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from apps.core.templatetags.ptbr import (
    banda_limite,
    brl,
    cnpj_mask,
    competencia,
    data,
    data_hora,
    percentual,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # The acceptance criterion, written exactly as the plan states it.
        (Decimal("1234.5"), "R$ 1.234,50"),
        (Decimal(0), "R$ 0,00"),
        (Decimal("0.05"), "R$ 0,05"),
        (Decimal("999.99"), "R$ 999,99"),
        (Decimal(1000), "R$ 1.000,00"),
        (Decimal(81000), "R$ 81.000,00"),
        (Decimal("1234567.89"), "R$ 1.234.567,89"),
        (Decimal("-42.5"), "-R$ 42,50"),
        (1234, "R$ 1.234,00"),
        (None, "—"),
        ("", "—"),
        ("nao-e-numero", "—"),
    ],
)
def test_brl_renders_brazilian_currency(value: object, expected: str) -> None:
    assert brl(value) == expected


def test_brl_never_routes_an_amount_through_float() -> None:
    """`Decimal(0.1)` is 0.1000000000000000055511151231257827; `Decimal("0.1")` is not.

    A float round-trip is invisible at two decimal places until it isn't, and the
    place it surfaces is a total that ends in .99 where the ledger says .00.
    """
    assert brl(0.1 + 0.2) == "R$ 0,30"
    assert brl(Decimal("2.675")) == "R$ 2,68"


def test_data_renders_day_first() -> None:
    assert data(date(2026, 2, 20)) == "20/02/2026"
    assert data(None) == "—"


def test_data_hora_converts_a_stored_utc_timestamp_to_brasilia() -> None:
    """Stored UTC, displayed BRT — three hours is a whole working morning."""
    stored = datetime(2026, 2, 20, 23, 30, tzinfo=UTC)
    assert data_hora(stored) == "20/02/2026 20:30"


def test_data_hora_crosses_the_date_boundary_correctly() -> None:
    """02:00 UTC is still the previous day in Brazil, and a due date cares."""
    stored = datetime(2026, 2, 21, 2, 0, tzinfo=UTC)
    assert data_hora(stored) == "20/02/2026 23:00"


def test_data_hora_leaves_a_naive_value_alone() -> None:
    """Guessing that a naive value is UTC would shift one that was already local."""
    naive = datetime(2026, 2, 20, 9, 15)  # noqa: DTZ001
    assert data_hora(naive) == "20/02/2026 09:15"


def test_data_hora_converts_from_any_zone_not_only_utc() -> None:
    stored = datetime(2026, 2, 20, 12, 0, tzinfo=ZoneInfo("Europe/Lisbon"))
    assert data_hora(stored) == "20/02/2026 09:00"


@pytest.mark.parametrize(
    ("month", "expected"),
    [
        (date(2026, 1, 1), "jan/2026"),
        (date(2026, 5, 1), "mai/2026"),
        (date(2026, 12, 1), "dez/2026"),
    ],
)
def test_competencia_names_the_month_in_portuguese(
    month: date,
    expected: str,
) -> None:
    assert competencia(month) == expected


def test_cnpj_mask_punctuates_both_formats() -> None:
    assert cnpj_mask("12345678000190") == "12.345.678/0001-90"
    # The 2026 alphanumeric registration: same fourteen positions, wider alphabet.
    assert cnpj_mask("AB345678000190") == "AB.345.678/0001-90"
    assert cnpj_mask("") == "—"


def test_percentual_uses_a_comma() -> None:
    assert percentual(Decimal("82.44")) == "82,4%"
    assert percentual(None) == "—"


def test_banda_limite_names_the_band_in_an_accountants_words() -> None:
    assert banda_limite("warning") == "perto do limite"
    assert banda_limite("exceeded_over_tolerance") == "excedido (acima de 20%)"
    # An unknown band is echoed rather than swallowed: silence would hide a new band.
    assert banda_limite("desconhecida") == "desconhecida"
