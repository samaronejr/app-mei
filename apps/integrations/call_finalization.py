"""Monotonic fenced finalization and idempotent attempt settlement."""

from dataclasses import replace
from datetime import timedelta

from django.utils import timezone

from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.integrations.call_contracts import (
    MAX_ATTEMPTS,
    CallResult,
    Claim,
    operation_for,
)
from apps.integrations.call_models import CallState, ProviderCall, ProviderCallAttempt
from apps.integrations.call_observations import meter, observe
from apps.integrations.call_state import current_epoch, uncertain


def finalize(ticket: Claim, result: CallResult) -> bool:
    """Keep stale replies as observations without rewriting a newer outcome."""
    assert_no_transaction()
    with tenant_context(ticket.tenant_id):
        call = ProviderCall.objects.select_for_update().get(
            pk=ticket.call_id, client_id=ticket.client_id
        )
        now = timezone.now()
        current = (
            call.fence == ticket.fence
            and call.lease_owner == ticket.owner
            and call.execution_epoch == ticket.execution_epoch
            and current_epoch() == ticket.execution_epoch
        )
        late = ProviderCallAttempt(
            attempt_id=ticket.owner,
            attempt_number=call.attempts,
            fence=ticket.fence,
            observation="late",
            reserved_microbrl=call.reserved_microbrl,
            occurred_at=call.dispatch_started_at or now,
        )
        if not current:
            observe(call, late, result)
            return False
        # A "result" observation exists exactly when settle() ran for the attempt.
        # A transmitted reply that cannot finalize stays a metered late receipt;
        # restore replay applies and settles its journaled result once.
        if call.state != CallState.STARTED:
            if (
                call.dispatch_started_at is not None
                and not ProviderCallAttempt.objects.filter(
                    call=call, attempt_id=ticket.owner, observation="result"
                ).exists()
            ):
                observe(call, late, result)
                meter(call, ticket.owner, result, late.occurred_at)
            return False
        if call.lease_until is None or call.lease_until <= now:
            observe(call, late, result)
            meter(call, ticket.owner, result, late.occurred_at)
            uncertain(call)
            return False
        attempt = late
        attempt.observation = "result"
        if result.state == CallState.RETRY:
            if call.operation_class != "read":
                result = replace(result, state=CallState.UNCERTAIN)
            elif call.attempts >= MAX_ATTEMPTS:
                result = replace(result, state=CallState.FAILED)
            else:
                call.next_attempt_at = now + timedelta(
                    seconds=(60, 300)[call.attempts - 1]
                )
        created = observe(call, attempt, result)
        if created:
            operation_for(call.operation).policy.settle(call, result)
            meter(call, ticket.owner, result, attempt.occurred_at)
        call.state = result.state
        call.http_status = result.http_status
        call.provider_code = result.provider_code
        call.billed = result.billed
        call.response_digest = result.response_digest
        call.settled_microbrl = result.settled_microbrl
        call.finalized_at = now
        call.version += 1
        call.updated_at = now
        call.save()
        if call.state == CallState.UNCERTAIN:
            uncertain(call)
        return True
