"""Public phased provider protocol; adapters never run in tenant transactions."""

from dataclasses import replace

from apps.integrations.budget_settlement import billing_result
from apps.integrations.call_contracts import MAX_ATTEMPTS, CallResult, Claim
from apps.integrations.call_dispatch import (
    Adapter,
    Dispatch,
    begin_io,
    record_result,
    transmit,
)
from apps.integrations.call_finalization import finalize
from apps.integrations.call_models import CallState
from apps.integrations.call_reservation import claim, reserve
from apps.integrations.serpro.reauthentication import schedule_client_retry

__all__ = [
    "Dispatch",
    "begin_io",
    "claim",
    "finalize",
    "perform",
    "record_result",
    "reserve",
]


def _perform_once(ticket: Claim, adapter: Adapter) -> CallResult:
    """Start once, transmit once, journal before settling the fenced DB result."""
    dispatch = begin_io(ticket)
    result = billing_result(dispatch.receipt, transmit(dispatch, adapter))
    if result.state == CallState.RETRY:
        if dispatch.receipt.operation_class != "read":
            result = replace(result, state=CallState.UNCERTAIN)
        elif dispatch.receipt.attempt_number >= MAX_ATTEMPTS:
            result = replace(result, state=CallState.FAILED)
    record_result(dispatch, result)
    if not finalize(ticket, result):
        return CallResult(CallState.UNCERTAIN, None, "stale_finalize", "desconhecido")
    return result


def perform(ticket: Claim, adapter: Adapter) -> CallResult:
    """Perform one send, with one fresh claim only after a definitive SERPRO 401."""
    result = _perform_once(ticket, adapter)
    if schedule_client_retry(ticket, result):
        retry = claim(ticket.tenant_id, ticket.call_id)
        if retry is not None:
            result = _perform_once(retry, adapter)
    from apps.integrations.serpro.work import own_client_result  # noqa: PLC0415

    own_client_result(ticket)
    return result
