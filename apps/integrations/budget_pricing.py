"""Exact micro-BRL pricing; an absent or malformed tariff is never free."""

from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Final

from django.conf import settings

from apps.integrations.call_contracts import (
    AdmissionDeniedError,
    CallResult,
    Reservation,
)
from apps.integrations.call_models import ProviderCall
from apps.integrations.journal_schema import JournalRecord

MICRO_BRL: Final = 1_000_000
MAX_MICRO_BRL: Final = 9_223_372_036_854_775_807
FOCUS_PLAN: Final = "focus-plan (PQ-FOCUS-4 pendente)"
# Explicit zero-cost tariff entries; a PQ-FOCUS-4 answer adds a new revision here.
ZERO_TARIFF_REVISIONS: Final = frozenset({FOCUS_PLAN})
BILLED_STATUSES: Final = frozenset({200, 202, 403})


class BudgetDeniedError(AdmissionDeniedError):
    """A missing tariff, missing cap or exhausted ceiling refuses admission."""

    def __init__(self, reason: str = "cota_esgotada") -> None:
        """Carry a minimized refusal code rather than configuration values."""
        self.reason = reason
        super().__init__(reason)


def microbrl(value: str | int | Decimal | None) -> int:
    """Parse a configured BRL amount without rounding or binary floating point."""
    try:
        amount = Decimal(str(value)) * MICRO_BRL
    except InvalidOperation:
        reason = "tarifa_ausente"
        raise BudgetDeniedError(reason) from None
    if (
        not amount.is_finite()
        or amount < 0
        or amount != amount.to_integral_value()
        or amount > MAX_MICRO_BRL
    ):
        reason = "tarifa_invalida"
        raise BudgetDeniedError(reason)
    return int(amount)


def price(call: ProviderCall, revision: str) -> Reservation:
    """Price the endpoint's billable class from the operation register."""
    if call.provider == "focus" and call.billing_class == "incluido_no_plano":
        if revision not in ZERO_TARIFF_REVISIONS:
            reason = "tarifa_ausente"
            raise BudgetDeniedError(reason)
        return Reservation(0, revision)
    if call.billing_class == "nao_faturada":
        return Reservation(0, "serpro-free-v1")
    metric = {
        ("serpro", "consulta"): "serpro_consulta",
        ("serpro", "emissao"): "serpro_emissao",
    }.get((call.provider, call.billing_class))
    if metric is None:
        reason = "tarifa_ausente"
        raise BudgetDeniedError(reason)
    amount = microbrl(settings.USAGE_UNIT_PRICES_BRL.get(metric))
    return Reservation(amount, revision)


def classify(call: ProviderCall | JournalRecord, result: CallResult) -> CallResult:
    """Apply endpoint billing independently of fiscal success or adapter hints."""
    if call.provider == "focus" or call.billing_class == "nao_faturada":
        return replace(result, billed="nao", settled_microbrl=0)
    if result.http_status is None:
        return replace(result, billed="desconhecido", settled_microbrl=None)
    billed = result.http_status in BILLED_STATUSES
    return replace(
        result,
        billed="sim" if billed else "nao",
        settled_microbrl=call.reserved_microbrl if billed else 0,
    )
