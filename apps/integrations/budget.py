"""Production admission policy with monthly tenant and platform spend ceilings."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Final

from django.conf import settings
from django.db import connection
from django.db.models import Sum
from django.utils import timezone

from apps.core.tasks import PlatformTask
from apps.core.tenancy import tenant_context
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.budget_pricing import BudgetDeniedError, microbrl, price
from apps.integrations.call_contracts import CallResult, Reservation
from apps.integrations.call_models import CallState, ProviderCall

BUDGET_LOCK: Final = 240110


def lock_budget() -> None:
    """Serialize cross-firm admission without bypassing either isolation layer."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [BUDGET_LOCK])


def total(budget: ProviderBudget) -> int:
    """Count known, uncertain and not-yet-sent exposures equally for admission."""
    return budget.known_liability + budget.uncertain_exposure + budget.reserved


def platform_total(provider: str, month: date) -> int:
    """Read each firm's balance under its own RLS context, including inactive firms."""
    amount = 0
    for tenant_id in PlatformTask.tenant_ids(include_inactive=True):
        with tenant_context(tenant_id):
            values = ProviderBudget.objects.filter(
                provider=provider, window_start=month
            ).aggregate(
                known=Sum("known_liability", default=0),
                uncertain=Sum("uncertain_exposure", default=0),
                held=Sum("reserved", default=0),
            )
            amount += values["known"] + values["uncertain"] + values["held"]
    return amount


def check_cap(
    budget: ProviderBudget, extra: int, *, focus_configuration: bool = False
) -> None:
    """Check both ceilings while the same transaction owns the global lock."""
    # An operator must still be able to disable upstream capture after overage.
    # These configuration calls reserve an explicit zero, never paid exposure.
    if focus_configuration and budget.provider == "focus" and extra == 0:
        return
    if total(budget) + extra > budget.cap_microbrl:
        raise BudgetDeniedError
    raw_cap = settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL.get(budget.provider)
    # Included Focus calls do not spend cash; overage alerts are tenant-local.
    if budget.provider == "focus" and extra == 0:
        return
    if raw_cap is None:
        reason = "cota_ausente"
        raise BudgetDeniedError(reason)
    global_cap = microbrl(raw_cap)
    if platform_total(budget.provider, budget.window_start) + extra > global_cap:
        raise BudgetDeniedError


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    """Combine a domain authority predicate with the mandatory budgeting policy."""

    authority: Callable[[ProviderCall], bool]

    def authorize(self, call: ProviderCall) -> bool:
        """Recheck the held reservation at claim and dispatch, not just enqueue."""
        if not self.authority(call):
            return False
        if call.state in {CallState.RESERVED, CallState.CLAIMED}:
            lock_budget()
            held = ProviderReservation.objects.filter(
                call=call, attempt_number=call.attempts + 1, state="reserved"
            ).first()
            if held is None:
                reason = "reserva_ausente"
                raise BudgetDeniedError(reason)
            budget = ProviderBudget.objects.select_for_update().get(pk=held.budget_id)
            if budget.window_start != timezone.now().date().replace(day=1):
                reason = "reserva_vencida"
                raise BudgetDeniedError(reason)
            check_cap(
                budget,
                0,
                focus_configuration=call.operation_class == "configuration",
            )
        return True

    def reserve(self, call: ProviderCall) -> Reservation:
        """Persist one per-attempt reservation before returning the claimable call."""
        lock_budget()
        budget = (
            ProviderBudget.objects.select_for_update()
            .filter(
                provider=call.provider,
                window_start=timezone.now().date().replace(day=1),
            )
            .first()
        )
        if budget is None:
            reason = "cota_ausente"
            raise BudgetDeniedError(reason)
        quoted = price(call, budget.tariff_revision)
        check_cap(
            budget,
            quoted.microbrl,
            focus_configuration=call.operation_class == "configuration",
        )
        ProviderReservation.objects.create(
            tenant_id=call.tenant_id,
            client_id=call.client_id,
            call=call,
            budget=budget,
            attempt_number=call.attempts + 1,
            microbrl=quoted.microbrl,
            tariff_revision=quoted.tariff_revision,
        )
        budget.reserved += quoted.microbrl
        budget.version += 1
        budget.save()
        from apps.integrations.budget_alerts import (  # noqa: PLC0415
            EXHAUSTED,
            NEAR,
            open_issue,
        )

        if total(budget) >= budget.cap_microbrl:
            open_issue(call.link, EXHAUSTED, budget.pk, service=call.operation)
        elif total(budget) * 5 >= budget.cap_microbrl * 4:
            open_issue(call.link, NEAR, budget.pk, service=call.operation)
        return quoted

    def settle(self, call: ProviderCall, result: CallResult) -> None:
        """Move a reservation once, including uncertain attempts proven billed."""
        from apps.integrations.budget_settlement import settle  # noqa: PLC0415

        settle(call, result)
