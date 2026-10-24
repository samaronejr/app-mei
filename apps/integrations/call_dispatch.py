"""The single dispatch-start commit, followed by independent storage and wire I/O."""

from dataclasses import dataclass, replace
from http.client import HTTPException
from typing import Protocol

from django.utils import timezone

from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.integrations import provider_journal
from apps.integrations.call_contracts import (
    AdmissionDeniedError,
    CallResult,
    Claim,
    IntentConflictError,
    StaleClaimError,
    operation_for,
)
from apps.integrations.call_models import CallState, ProviderCall, ProviderEffectBarrier
from apps.integrations.call_state import admit, current_epoch, lock_controls, uncertain
from apps.integrations.journal_schema import JournalRecord


@dataclass(frozen=True, slots=True)
class Dispatch:
    """One already-authorized attempt; response bytes never enter the protocol."""

    claim: Claim
    receipt: JournalRecord
    journaled: bool


class Adapter(Protocol):
    """Transport performs exactly one request; it must not retry internally."""

    def send(self, dispatch: Dispatch) -> CallResult:
        """Return minimized result codes after optional protected-object ingestion."""
        ...


class StateReader(Protocol):
    """Read-only provider reconciliation, never a repeat of the initiating effect.

    Provider adapters expose this method through a separately registered read
    operation's send path. That call still needs its own claim and fence (and
    reservation/journal when billed). Database restore never invokes it.
    """

    def read_state(self, dispatch: Dispatch) -> CallResult:
        """Read provider evidence for an uncertain business effect."""
        ...


def _prepare(ticket: Claim) -> Dispatch | AdmissionDeniedError | StaleClaimError:
    link_id = ProviderCall.objects.values_list("link_id", flat=True).get(
        pk=ticket.call_id, client_id=ticket.client_id
    )
    tenant, link = lock_controls(ticket.tenant_id, link_id)
    call = ProviderCall.objects.select_for_update().get(
        pk=ticket.call_id, client_id=ticket.client_id
    )
    call.link = link
    if (
        call.fence != ticket.fence
        or call.lease_owner != ticket.owner
        or call.execution_epoch != ticket.execution_epoch
        or current_epoch() != ticket.execution_epoch
        or call.state != CallState.CLAIMED
        or call.dispatch_started_at is not None
    ):
        return StaleClaimError()
    operation = operation_for(call.operation)
    if call.lease_until is None or call.lease_until <= timezone.now():
        uncertain(call)
        return StaleClaimError()
    try:
        admit(call, tenant, operation)
    except AdmissionDeniedError as error:
        call.state = (
            CallState.PAUSED if str(error) == CallState.PAUSED else CallState.AUTHORITY
        )
        call.version += 1
        call.save()
        return error
    # Policy/authority locks may have blocked past the lease deadline.
    if call.lease_until <= timezone.now():
        uncertain(call)
        return StaleClaimError()
    call.attempts += 1
    call.dispatch_started_at = timezone.now()
    call.x_request_tag = ticket.owner
    receipt = JournalRecord.started(call, ticket)
    if operation.operation_class in {"issuance", "registration", "configuration"}:
        barrier, created = ProviderEffectBarrier.objects.get_or_create(
            tenant_id=ticket.tenant_id,
            client_id=ticket.client_id,
            provider=call.provider,
            business_effect_key=call.business_effect_key,
            defaults={
                "request_digest": call.request_digest,
                "first_start_receipt": receipt.path,
                "call": call,
            },
        )
        if not created:
            if barrier.request_digest != call.request_digest:
                raise IntentConflictError
            uncertain(call)
            return StaleClaimError()
    call.state = CallState.STARTED
    call.version += 1
    call.save()
    return Dispatch(ticket, receipt, operation.journaled)


def begin_io(ticket: Claim) -> Dispatch:
    """Authorize once under locks, commit, then durably record intent outside them."""
    assert_no_transaction()
    with tenant_context(ticket.tenant_id):
        prepared = _prepare(ticket)
    if isinstance(prepared, (AdmissionDeniedError, StaleClaimError)):
        raise prepared
    dispatch = prepared
    if dispatch.journaled:
        provider_journal.append(dispatch.receipt)
    return dispatch


def record_result(dispatch: Dispatch, result: CallResult) -> None:
    """Durably record the reply before the fenced database finalization."""
    assert_no_transaction()
    if dispatch.journaled:
        provider_journal.append(replace(dispatch.receipt, result=result))


def transmit(dispatch: Dispatch, adapter: Adapter) -> CallResult:
    """One network transmission; all transport ambiguity remains uncertain."""
    assert_no_transaction()
    try:
        return adapter.send(dispatch)
    except (OSError, HTTPException):
        return CallResult(CallState.UNCERTAIN, None, "connection_lost", "desconhecido")
