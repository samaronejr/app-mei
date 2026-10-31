"""A complete SERPRO 401 alone permits one newly fenced and budgeted attempt."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.integrations.call_contracts import CallResult, Claim, StaleClaimError
from apps.integrations.call_models import (
    CallState,
    FirmProviderCall,
    FirmProviderCallAttempt,
    ProviderCall,
    ProviderCallAttempt,
)
from apps.integrations.call_state import current_epoch

if TYPE_CHECKING:
    from apps.integrations.firm_calls import FirmClaim

COMPLETE_401: Final = "serpro_401"
FIRST_ATTEMPT: Final = 1


def definitive_401(result: CallResult) -> bool:
    """Reject errors that merely saw 401 headers without a complete response."""
    return (
        result.http_status == 401  # noqa: PLR2004 - the explicit protocol exception
        and result.provider_code == COMPLETE_401
        and result.state == CallState.FAILED
        and result.billed == "nao"
    )


def prior_401(call: ProviderCall | FirmProviderCall) -> bool:
    """Prove the prior result under this same call, not another effect or late reply."""
    observations = (
        ProviderCallAttempt.objects.filter(call=call, client_id=call.client_id)
        if isinstance(call, ProviderCall)
        else FirmProviderCallAttempt.objects.filter(call=call)
    )
    return observations.filter(
        attempt_number=FIRST_ATTEMPT,
        observation="result",
        outcome=CallState.FAILED,
        http_status=401,
        provider_code=COMPLETE_401,
    ).exists()


def schedule_client_retry(ticket: Claim, result: CallResult) -> bool:
    """Reopen only the current first definitive rejection, retaining the request key."""
    if not definitive_401(result):
        return False
    with tenant_context(ticket.tenant_id):
        call = ProviderCall.objects.select_for_update().get(
            pk=ticket.call_id, client_id=ticket.client_id
        )
        if (
            call.provider != "serpro"
            or call.state != CallState.FAILED
            or call.attempts != FIRST_ATTEMPT
            or call.fence != ticket.fence
            or call.lease_owner != ticket.owner
            or call.execution_epoch != ticket.execution_epoch
            or current_epoch() != ticket.execution_epoch
            or not prior_401(call)
        ):
            return False
        if call.lease_until is None or call.lease_until <= timezone.now():
            raise StaleClaimError
        call.state = CallState.RETRY
        call.next_attempt_at = timezone.now()
        call.version += 1
        call.save()
        return True


def schedule_firm_retry(ticket: FirmClaim, result: CallResult) -> bool:
    """Reopen a definitely rejected termo, never an ambiguously accepted one."""
    if not definitive_401(result):
        return False
    with tenant_context(ticket.tenant_id):
        call = FirmProviderCall.objects.select_for_update().get(pk=ticket.call_id)
        if (
            call.provider != "serpro"
            or call.state != CallState.FAILED
            or call.attempts != FIRST_ATTEMPT
            or call.fence != ticket.fence
            or call.lease_owner != ticket.owner
            or call.execution_epoch != ticket.execution_epoch
            or current_epoch() != ticket.execution_epoch
            or not prior_401(call)
        ):
            return False
        if call.lease_until is None or call.lease_until <= timezone.now():
            raise StaleClaimError
        call.state = CallState.RETRY
        call.version += 1
        call.save()
        return True
