"""Public phased provider protocol; adapters never run in tenant transactions."""

from dataclasses import replace

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

__all__ = [
    "Dispatch",
    "begin_io",
    "claim",
    "finalize",
    "perform",
    "record_result",
    "reserve",
]


def perform(ticket: Claim, adapter: Adapter) -> CallResult:
    """Start once, transmit once, journal before settling the fenced DB result."""
    dispatch = begin_io(ticket)
    result = transmit(dispatch, adapter)
    if result.state == CallState.RETRY:
        if dispatch.receipt.operation_class != "read":
            result = replace(result, state=CallState.UNCERTAIN)
        elif dispatch.receipt.attempt_number >= MAX_ATTEMPTS:
            result = replace(result, state=CallState.FAILED)
    record_result(dispatch, result)
    if not finalize(ticket, result):
        return CallResult(CallState.UNCERTAIN, None, "stale_finalize", "desconhecido")
    return result
