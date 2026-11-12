"""AXRM-010 admission for zero-cost, client-less firm authorization attempts."""

from collections.abc import Callable
from dataclasses import dataclass

from django.utils import timezone

from apps.integrations.budget import check_cap, lock_budget
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.budget_pricing import BudgetDeniedError
from apps.integrations.call_models import CallState, FirmProviderCall
from apps.integrations.serpro.models import FirmProviderReservation


@dataclass(frozen=True, slots=True)
class FirmBudgetPolicy:
    """A free tariff still needs explicit firm and platform budget admission."""

    authority: Callable[[FirmProviderCall], bool]

    def authorize(self, call: FirmProviderCall) -> bool:
        """Recheck the domain authority and this attempt's held zero reservation."""
        if not self.authority(call):
            return False
        if call.state in {CallState.RESERVED, CallState.CLAIMED}:
            lock_budget()
            held = FirmProviderReservation.objects.filter(
                call=call, attempt_number=call.attempts + 1, state="reserved"
            ).first()
            if held is None:
                reason = "reserva_ausente"
                raise BudgetDeniedError(reason)
            budget = ProviderBudget.objects.select_for_update().get(pk=held.budget_id)
            if budget.window_start != timezone.now().date().replace(day=1):
                reason = "reserva_vencida"
                raise BudgetDeniedError(reason)
            check_cap(budget, 0)
        return True

    def reserve(self, call: FirmProviderCall) -> None:
        """Write a distinct reservation before each admitted firm attempt."""
        lock_budget()
        budget = (
            ProviderBudget.objects.select_for_update()
            .filter(
                provider="serpro", window_start=timezone.now().date().replace(day=1)
            )
            .first()
        )
        if budget is None:
            reason = "cota_ausente"
            raise BudgetDeniedError(reason)
        check_cap(budget, 0)
        FirmProviderReservation.objects.create(
            tenant_id=call.tenant_id,
            call=call,
            budget=budget,
            attempt_number=call.attempts + 1,
        )
        budget.version += 1
        budget.save()

    def settle(self, call: FirmProviderCall) -> None:
        """Apoiar is unbilled for every status; release its zero reservation once."""
        held = (
            FirmProviderReservation.objects.select_for_update()
            .filter(call=call, attempt_number=call.attempts, state="reserved")
            .first()
        )
        if held is not None:
            held.state = "released"
            held.version += 1
            held.save()

    def release(self, call: FirmProviderCall) -> None:
        """Return a zero reservation when dispatch refused before sending a byte."""
        for held in FirmProviderReservation.objects.select_for_update().filter(
            call=call, state="reserved"
        ):
            held.state = "released"
            held.version += 1
            held.save()
