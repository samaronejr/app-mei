"""The single dispatch-start commit, followed by independent storage and wire I/O."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from http.client import HTTPException
from typing import TYPE_CHECKING, Protocol

from django.utils import timezone

from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.integrations import provider_journal
from apps.integrations.budget_alerts import refused
from apps.integrations.budget_pricing import BudgetDeniedError
from apps.integrations.budget_settlement import billing_result, release, release_unsent
from apps.integrations.call_contracts import (
    AdmissionDeniedError,
    CallResult,
    Claim,
    IntentConflictError,
    StaleClaimError,
    operation_for,
)
from apps.integrations.call_models import (
    CallState,
    FirmProviderCall,
    ProviderCall,
    ProviderEffectBarrier,
)
from apps.integrations.call_state import admit, current_epoch, lock_controls, uncertain
from apps.integrations.journal_schema import JournalRecord
from apps.integrations.transport_types import EnvioNaoAutorizado

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from apps.integrations.firm_calls import FirmClaim, FirmDispatch


@dataclass(frozen=True, slots=True)
class Dispatch:
    """One already-authorized attempt; response bytes never enter the protocol."""

    claim: Claim
    receipt: JournalRecord
    journaled: bool


@dataclass(slots=True)
class _WirePermit:
    provider: str
    environment: str
    operation: str
    used: bool = False


# Dispatches that begin_io() authorized and journaled in this process and that
# transmit() has not consumed yet. Only an exact issued dispatch opens the wire.
_ISSUED: dict[tuple[UUID, int, UUID], Dispatch | FirmDispatch] = {}
_WIRE: ContextVar[_WirePermit | None] = ContextVar("provider_wire", default=None)


def _issued_key(ticket: Claim | FirmClaim) -> tuple[UUID, int, UUID]:
    return (ticket.call_id, ticket.fence, ticket.owner)


def issue_dispatch(dispatch: Dispatch | FirmDispatch) -> None:
    """Register an exact client or firm dispatch after its journal commit."""
    _ISSUED[_issued_key(dispatch.claim)] = dispatch


@contextmanager
def _wire_window(provider: str, environment: str, operation: str) -> Iterator[None]:
    token = _WIRE.set(_WirePermit(provider, environment, operation))
    try:
        yield
    finally:
        _WIRE.reset(token)


def open_wire(provider: str, environment: str, operation: str) -> None:
    """Consume the one transmission the current dispatch allows, or refuse."""
    permit = _WIRE.get()
    if (
        permit is None
        or permit.used
        or (permit.provider, permit.environment, permit.operation)
        != (provider, environment, operation)
    ):
        raise EnvioNaoAutorizado
    permit.used = True


def require_dispatch(provider: str) -> None:
    """Per-call credentials exist only for a dispatch of the same provider."""
    permit = _WIRE.get()
    if permit is None or permit.provider != provider:
        raise EnvioNaoAutorizado


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
        release(call)
        if isinstance(error, BudgetDeniedError):
            call.state = CallState.QUOTA
            call.provider_code = error.reason
            refused(call)
        else:
            call.state = (
                CallState.PAUSED
                if str(error) == CallState.PAUSED
                else CallState.AUTHORITY
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
    issue_dispatch(dispatch)
    return dispatch


def record_result(dispatch: Dispatch, result: CallResult) -> None:
    """Durably record the reply before the fenced database finalization."""
    assert_no_transaction()
    if dispatch.journaled:
        provider_journal.append(
            replace(dispatch.receipt, result=billing_result(dispatch.receipt, result))
        )


def _claim_holds(ticket: Claim | FirmClaim) -> bool:
    """Report whether the ticket still owns the started call under a live lease."""
    with tenant_context(ticket.tenant_id):
        calls = (
            ProviderCall.objects.filter(client_id=ticket.client_id)
            if isinstance(ticket, Claim)
            else FirmProviderCall.objects.all()
        )
        return (
            current_epoch() == ticket.execution_epoch
            and calls.filter(
                pk=ticket.call_id,
                state=CallState.STARTED,
                fence=ticket.fence,
                lease_owner=ticket.owner,
                execution_epoch=ticket.execution_epoch,
                lease_until__gt=timezone.now(),
            ).exists()
        )


@contextmanager
def dispatch_window(dispatch: Dispatch | FirmDispatch) -> Iterator[None]:
    """Consume one journaled dispatch, recheck it, then authorize its wire phase."""
    assert_no_transaction()
    key = _issued_key(dispatch.claim)
    issued = _ISSUED.pop(key, None)
    if issued is None:
        raise StaleClaimError
    if issued != dispatch:
        _ISSUED[key] = issued
        raise StaleClaimError
    # Popped first, so a stale dispatch can never be transmitted later either.
    if not _claim_holds(issued.claim):
        if isinstance(issued, Dispatch):
            with tenant_context(issued.claim.tenant_id):
                release_unsent(issued.receipt)
        raise StaleClaimError
    receipt = issued.receipt
    with _wire_window(receipt.provider, receipt.environment, receipt.operation):
        yield


def transmit(dispatch: Dispatch, adapter: Adapter) -> CallResult:
    """Transmit one issued client dispatch; transport ambiguity stays uncertain."""
    try:
        with dispatch_window(dispatch):
            return adapter.send(dispatch)
    except (OSError, HTTPException):
        return CallResult(CallState.UNCERTAIN, None, "connection_lost", "desconhecido")
