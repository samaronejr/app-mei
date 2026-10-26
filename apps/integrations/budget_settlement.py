"""Move per-attempt balances without losing uncertainty or double billing replay."""

from typing import Final

from apps.integrations.budget import BudgetPolicy, lock_budget
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.budget_pricing import BudgetDeniedError, classify
from apps.integrations.call_contracts import CallResult, operation_for
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.journal_schema import JournalRecord

BUCKETS = {
    "reserved": "reserved",
    "known": "known_liability",
    "uncertain": "uncertain_exposure",
}
# An observed billed or unbilled outcome is final for its attempt: a later
# quarantine or replay may change the call's state, never this attempt's money.
SETTLED: Final = frozenset({"known", "released"})


def billing_result(
    call: ProviderCall | JournalRecord, result: CallResult
) -> CallResult:
    """Apply tariff classification to production budget policies."""
    if isinstance(operation_for(call.operation).policy, BudgetPolicy):
        return classify(call, result)
    return result


def _move(held: ProviderReservation, target: str) -> None:
    budget = ProviderBudget.objects.select_for_update().get(pk=held.budget_id)
    if held.state == target:
        return
    previous_bucket = BUCKETS.get(held.state)
    next_bucket = BUCKETS.get(target)
    if previous_bucket is not None:
        setattr(
            budget, previous_bucket, getattr(budget, previous_bucket) - held.microbrl
        )
    if next_bucket is not None:
        setattr(budget, next_bucket, getattr(budget, next_bucket) + held.microbrl)
    budget.version += 1
    budget.save()
    held.state = target
    held.version += 1
    held.save()


def settle(call: ProviderCall, result: CallResult) -> None:
    """Restore the original attempt if needed, then replace its accounting bucket."""
    lock_budget()
    held = (
        ProviderReservation.objects.select_for_update()
        .filter(call=call, attempt_number=call.attempts)
        .first()
    )
    rebuilt = held is None
    if held is None:
        # A journaled call can be newer than the restored DB; its original month
        # and quoted amount survive independently in the receipt.
        if call.dispatch_started_at is None:
            reason = "reserva_ausente"
            raise BudgetDeniedError(reason)
        budget = (
            ProviderBudget.objects.select_for_update()
            .filter(
                provider=call.provider,
                window_start=call.dispatch_started_at.date().replace(day=1),
            )
            .first()
        )
        if budget is None:
            reason = "cota_ausente"
            raise BudgetDeniedError(reason)
        held = ProviderReservation.objects.create(
            tenant_id=call.tenant_id,
            client_id=call.client_id,
            call=call,
            budget=budget,
            attempt_number=call.attempts,
            microbrl=call.reserved_microbrl,
            tariff_revision=call.tariff_revision,
            state="released",
        )
    result = classify(call, result)
    target = {"sim": "known", "nao": "released", "desconhecido": "uncertain"}[
        result.billed
    ]
    # Only a still-reserved or uncertain attempt moves; a rebuilt receipt starts
    # from "released" (no bucket) and takes its journaled outcome once.
    if held.state in SETTLED and not rebuilt:
        return
    _move(held, target)


def release(call: ProviderCall) -> None:
    """Only an untransmitted reservation can be safely returned to the budget."""
    lock_budget()
    held = (
        ProviderReservation.objects.select_for_update()
        .filter(call=call, attempt_number=call.attempts + 1, state="reserved")
        .first()
    )
    if held is not None:
        _move(held, "released")


def release_unsent(receipt: JournalRecord) -> None:
    """Release only the issued attempt proven refused before its first byte.

    Another worker's claim may have quarantined this attempt. A settled bill or a
    newer attempt's reservation must never be rewritten by this old dispatch.
    """
    lock_budget()
    held = (
        ProviderReservation.objects.select_for_update()
        .filter(
            call_id=receipt.call_id,
            client_id=receipt.client_id,
            attempt_number=receipt.attempt_number,
            state__in=("reserved", "uncertain"),
        )
        .first()
    )
    if held is not None:
        _move(held, "released")


def quarantine(call: ProviderCall) -> None:
    """Release an unsent lease; retain maximum exposure after a started attempt."""
    if not isinstance(operation_for(call.operation).policy, BudgetPolicy):
        return
    if call.dispatch_started_at is None:
        release(call)
    else:
        settle(
            call, CallResult(CallState.UNCERTAIN, None, "lease_lost", "desconhecido")
        )
