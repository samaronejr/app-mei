"""The single dispatch-start commit, followed by independent storage and wire I/O."""

from __future__ import annotations

from contextlib import AbstractContextManager, ExitStack, contextmanager
from contextvars import ContextVar
from copy import copy
from dataclasses import dataclass, field, replace
from functools import partial
from http.client import HTTPException
from os import getpid
from threading import Lock
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
    ProviderCallAttempt,
    ProviderEffectBarrier,
)
from apps.integrations.call_observations import meter, observe
from apps.integrations.call_state import admit, current_epoch, lock_controls, uncertain
from apps.integrations.call_unopened import (
    UNOPENED_RESULT,
    _issue_unopened_write,
    is_unopened,
    prior_unopened,
    rewind_unopened,
)
from apps.integrations.journal_schema import JournalRecord
from apps.integrations.serpro.reauthentication import prior_401
from apps.integrations.transport_types import EnvioNaoAutorizado

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from datetime import datetime
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
    lease_until: datetime
    used: bool = False
    expired: bool = False
    guard: Callable[[], AbstractContextManager[None]] | None = None
    scope: ExitStack = field(default_factory=ExitStack, repr=False)
    issued: Dispatch | FirmDispatch | None = None
    admitting: bool = False
    unopened: bool = False
    admission_lock: Lock = field(default_factory=Lock, repr=False)
    process_id: int = field(default_factory=getpid)


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
def _wire_window(
    provider: str,
    environment: str,
    operation: str,
    lease_until: datetime,
    *,
    guard: Callable[[], AbstractContextManager[None]] | None = None,
) -> Iterator[_WirePermit]:
    permit = _WirePermit(provider, environment, operation, lease_until, guard=guard)
    token = _WIRE.set(permit)
    try:
        yield permit
    finally:
        try:
            permit.scope.close()
        finally:
            _WIRE.reset(token)


def open_wire(provider: str, environment: str, operation: str) -> None:
    """Consume the one transmission the current dispatch allows, or refuse.

    The lease is the last check before the socket opens: a lease that expires
    after the transmit recheck consumes the permit and sends nothing.
    """
    permit = _WIRE.get()
    if (
        permit is None
        or permit.used
        or (permit.provider, permit.environment, permit.operation)
        != (provider, environment, operation)
        or (operation == "SP-SIT-R" and permit.guard is None)
    ):
        raise EnvioNaoAutorizado
    permit.used = True
    if timezone.now() >= permit.lease_until:
        permit.expired = True
        raise StaleClaimError
    if permit.guard is not None:
        with permit.admission_lock:
            permit.admitting = True
        try:
            permit.scope.enter_context(permit.guard())
        finally:
            with permit.admission_lock:
                permit.admitting = False
        if permit.unopened:
            raise EnvioNaoAutorizado
        assert_no_transaction()


def close_wire() -> None:
    """End the operation permit before adapter result or private-document work."""
    permit = _WIRE.get()
    if permit is not None:
        permit.scope.close()


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
    unopened = prior_unopened(call)
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
            # The barrier stays immutable. A complete 401 or this same call's
            # exact latest no-wire closure proves the effect was not repeated.
            if not (
                call.provider == "serpro"
                and barrier.call_id == call.pk
                and (
                    unopened
                    or (
                        call.attempts == 2  # noqa: PLR2004 - one auth retry
                        and prior_401(call)
                    )
                )
            ):
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
    if is_unopened(dispatch.receipt.operation, result):
        _require_unopened(dispatch)
        record = replace(dispatch.receipt, result=result)
        with _issue_unopened_write(record):
            provider_journal.append(record)
    elif dispatch.journaled:
        provider_journal.append(
            replace(dispatch.receipt, result=billing_result(dispatch.receipt, result))
        )


def _require_unopened(dispatch: Dispatch) -> None:
    """Consume exactly one local closure before admission can hand off to I/O."""
    permit = _WIRE.get()
    if permit is None or permit.process_id != getpid():
        raise EnvioNaoAutorizado
    with permit.admission_lock:
        if (
            not permit.admitting
            or not dispatch.journaled
            or permit.unopened
            or permit.issued is not dispatch
            or dispatch.receipt.operation != "SP-SIT-R"
        ):
            raise EnvioNaoAutorizado
        permit.unopened = True


def abort_unopened(dispatch: Dispatch) -> None:
    """Close a veto durably while its own guard is still before the first byte."""
    record_result(dispatch, UNOPENED_RESULT)
    receipt = replace(dispatch.receipt, result=UNOPENED_RESULT)
    ticket = dispatch.claim
    with tenant_context(ticket.tenant_id):
        lock_controls(ticket.tenant_id, receipt.link_id)
        call = ProviderCall.objects.select_for_update().get(
            pk=ticket.call_id, client_id=ticket.client_id
        )
        observe(
            call,
            ProviderCallAttempt(
                attempt_id=ticket.owner,
                attempt_number=receipt.attempt_number,
                fence=ticket.fence,
                observation="result",
                reserved_microbrl=receipt.reserved_microbrl,
                occurred_at=receipt.occurred_at,
            ),
            UNOPENED_RESULT,
        )
        attempt = copy(call)
        attempt.attempts = receipt.attempt_number
        attempt.fence = receipt.fence
        meter(attempt, ticket.owner, UNOPENED_RESULT, receipt.occurred_at)
        rewind_unopened(call, receipt)


def _live_lease(ticket: Claim | FirmClaim) -> datetime | None:
    """Return the lease deadline while the ticket still owns the started call.

    A short read-only tenant transaction (RLS needs its transaction-local tenant
    setting), committed before any I/O: it takes only the shared execution-epoch
    advisory lock, never a row lock, and nothing is held during the send.
    """
    with tenant_context(ticket.tenant_id):
        if current_epoch() != ticket.execution_epoch:
            return None
        calls = (
            ProviderCall.objects.filter(client_id=ticket.client_id)
            if isinstance(ticket, Claim)
            else FirmProviderCall.objects.all()
        )
        return (
            calls.filter(
                pk=ticket.call_id,
                state=CallState.STARTED,
                fence=ticket.fence,
                lease_owner=ticket.owner,
                execution_epoch=ticket.execution_epoch,
                lease_until__gt=timezone.now(),
            )
            .values_list("lease_until", flat=True)
            .first()
        )


def _release_unsent(issued: Dispatch | FirmDispatch) -> None:
    """Return a refused client attempt's reservation; firm calls hold none."""
    if isinstance(issued, Dispatch):
        with tenant_context(issued.claim.tenant_id):
            release_unsent(issued.receipt)


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
    lease_until = _live_lease(issued.claim)
    if lease_until is None:
        _release_unsent(issued)
        raise StaleClaimError
    receipt = issued.receipt
    factory = (
        operation_for(receipt.operation).wire_guard
        if isinstance(issued, Dispatch)
        else None
    )
    guard = (
        partial(factory, issued)
        if factory is not None and isinstance(issued, Dispatch)
        else None
    )
    with _wire_window(
        receipt.provider,
        receipt.environment,
        receipt.operation,
        lease_until,
        guard=guard,
    ) as permit:
        permit.issued = issued
        try:
            yield
        finally:
            # open_wire() refused the first byte: nothing was sent.
            if permit.expired:
                _release_unsent(issued)


def transmit(dispatch: Dispatch, adapter: Adapter) -> CallResult:
    """Transmit one issued client dispatch; transport ambiguity stays uncertain."""
    try:
        with dispatch_window(dispatch):
            return adapter.send(dispatch)
    except (OSError, HTTPException):
        return CallResult(CallState.UNCERTAIN, None, "connection_lost", "desconhecido")
