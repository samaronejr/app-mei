"""SP-TERMO firm submissions: the A7.4 phases without a client (todo 9).

The termo de autorizacao is a firm-level registration (A6 SP-TERMO): it is never a
`ProviderCall` (which requires a client), and no client is fabricated for it. Each
signed `FirmAuthorization` has at most one submission; the submission is reserved,
claimed under a lease and fence, journaled independently, transmitted once outside
any transaction and finalized under its fence. Todo 13 registers the real adapter
and policy; until then no operation is registered and every reservation refuses.
"""

from dataclasses import dataclass, replace
from datetime import timedelta
from http.client import HTTPException
from typing import Final, Literal, Protocol
from uuid import UUID, uuid4

from django.conf import settings
from django.utils import timezone

from apps.core.flags import tenant_row_has
from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.integrations import provider_journal
from apps.integrations.call_contracts import (
    AdmissionDeniedError,
    CallRequest,
    CallResult,
    IntentConflictError,
    StaleClaimError,
)
from apps.integrations.call_dispatch import dispatch_window, issue_dispatch
from apps.integrations.call_models import (
    CallState,
    FirmProviderCall,
    FirmProviderCallAttempt,
)
from apps.integrations.call_state import current_epoch
from apps.integrations.journal_schema import FirmJournalRecord
from apps.integrations.models import FirmAuthorization, ProviderConnection
from apps.integrations.serpro.firm_budget import FirmBudgetPolicy
from apps.integrations.serpro.reauthentication import prior_401, schedule_firm_retry
from apps.integrations.serpro.work import own_firm_result
from apps.tenants.models import Tenant

LEASE: Final = timedelta(seconds=120)
FIRM_OPERATION_CODES: Final = frozenset({"SP-TERMO"})


class FirmPolicy(Protocol):
    """Firm-level authority (todo 13: capability and signed-termo checks)."""

    def authorize(self, call: FirmProviderCall) -> bool:
        """Recheck the firm's authority to submit under the control locks."""
        ...


@dataclass(frozen=True, slots=True)
class FirmOperation:
    """A closed firm operation with its mandatory policy hook."""

    code: Literal["SP-TERMO"]
    provider: Literal["serpro"]
    revision: str
    policy: FirmPolicy


@dataclass(frozen=True, slots=True)
class FirmClaim:
    """A committed lease on one firm submission; dispatch still rechecks it."""

    tenant_id: UUID
    call_id: UUID
    fence: int
    owner: UUID
    execution_epoch: int


@dataclass(frozen=True, slots=True)
class FirmDispatch:
    """One authorized attempt; response bytes never enter the protocol."""

    claim: FirmClaim
    receipt: FirmJournalRecord


class FirmAdapter(Protocol):
    """Transport performs exactly one request; it must not retry internally."""

    def send(self, dispatch: FirmDispatch) -> CallResult:
        """Return minimized result codes only."""
        ...


# Deliberately empty at startup: no termo can be submitted before todo 13.
_FIRM_OPERATIONS: dict[str, FirmOperation] = {}


def register_firm_operation(operation: FirmOperation) -> None:
    """Register the firm-level operation once; nothing else is a firm call."""
    if operation.code not in FIRM_OPERATION_CODES or operation.code in _FIRM_OPERATIONS:
        message = "Operation cannot be registered as a firm provider call."
        raise ValueError(message)
    _FIRM_OPERATIONS[operation.code] = operation


def _operation(code: str) -> FirmOperation:
    try:
        return _FIRM_OPERATIONS[code]
    except KeyError:
        message = "Firm operation has no registered admission policy."
        raise AdmissionDeniedError(message) from None


def _lock(tenant_id: UUID, connection_id: UUID) -> tuple[Tenant, ProviderConnection]:
    """Lock the tenant entitlement, then the connection (the client-call order)."""
    # PLATFORM_QUERY_OK: lock exactly the explicit tenant's feature entitlement.
    tenant = Tenant.objects.select_for_update(no_key=True).get(pk=tenant_id)
    connection = ProviderConnection.objects.select_for_update().get(pk=connection_id)
    return tenant, connection


def _admit(
    call: FirmProviderCall,
    tenant: Tenant,
    connection: ProviderConnection,
    operation: FirmOperation,
) -> None:
    if (
        settings.PROVIDERS_PAUSED
        or getattr(settings, f"{call.provider.upper()}_PAUSED")
        or not tenant.is_active
        or not tenant_row_has(tenant, "federal_checks")
        or connection.status != "ativa"
        or call.environment != getattr(settings, f"{call.provider.upper()}_ENVIRONMENT")
        or (call.environment == "producao" and not settings.PROVIDER_REAL_DATA_APPROVED)
    ):
        raise AdmissionDeniedError(CallState.PAUSED)
    if (
        connection.provider != operation.provider
        or call.operation_revision != operation.revision
        or call.connection_version != connection.version
        or not operation.policy.authorize(call)
    ):
        raise AdmissionDeniedError(CallState.AUTHORITY)


def _uncertain(call: FirmProviderCall) -> None:
    """Quarantine the registration and open client-less firm recovery work."""
    policy = _operation(call.operation).policy
    if isinstance(policy, FirmBudgetPolicy):
        if call.dispatch_started_at is None:
            policy.release(call)
        else:
            policy.settle(call)
    call.state = CallState.UNCERTAIN
    call.version += 1
    call.updated_at = timezone.now()
    call.save()
    own_firm_result(call)


def observe_firm(
    call: FirmProviderCall, attempt: FirmProviderCallAttempt, result: CallResult
) -> bool:
    """Append one result association once; a conflicting replay is refused."""
    observed, created = FirmProviderCallAttempt.objects.get_or_create(
        tenant_id=call.tenant_id,
        call=call,
        attempt_id=attempt.attempt_id,
        observation=attempt.observation,
        defaults={
            "attempt_number": attempt.attempt_number,
            "fence": attempt.fence,
            "outcome": result.state,
            "http_status": result.http_status,
            "provider_code": result.provider_code,
            "response_digest": result.response_digest,
            "occurred_at": attempt.occurred_at,
        },
    )
    if not created and (
        observed.outcome != result.state
        or observed.http_status != result.http_status
        or observed.provider_code != result.provider_code
        or observed.response_digest != result.response_digest
        or observed.fence != attempt.fence
    ):
        raise IntentConflictError
    return created


def firm_reserve(
    tenant_id: UUID, authorization_id: UUID, connection_id: UUID, request: CallRequest
) -> FirmProviderCall:
    """Return the identical submission, or reserve the signed termo's only one."""
    assert_no_transaction()
    operation = _operation(request.operation)
    with tenant_context(tenant_id):
        tenant, connection = _lock(tenant_id, connection_id)
        existing = (
            FirmProviderCall.objects.select_for_update()
            .filter(request_key=request.request_key)
            .first()
        )
        if existing is not None:
            if (
                existing.request_digest != request.request_digest
                or existing.authorization_id != authorization_id
                or existing.connection_id != connection.pk
                or existing.operation != operation.code
                or existing.operation_revision != operation.revision
            ):
                raise IntentConflictError
            return existing
        if FirmProviderCall.objects.filter(authorization_id=authorization_id).exists():
            raise IntentConflictError
        call = FirmProviderCall(
            tenant_id=tenant_id,
            authorization=FirmAuthorization.objects.get(pk=authorization_id),
            connection=connection,
            operation=operation.code,
            operation_revision=operation.revision,
            provider=connection.provider,
            environment=connection.environment,
            request_key=request.request_key,
            request_digest=request.request_digest,
            connection_version=connection.version,
        )
        _admit(call, tenant, connection, operation)
        call.save()
        if isinstance(operation.policy, FirmBudgetPolicy):
            operation.policy.reserve(call)
        call.state = CallState.RESERVED
        call.save()
        return call


def firm_claim(tenant_id: UUID, call_id: UUID) -> FirmClaim | None:
    """Commit one lease; a registration has exactly one transmission slot."""
    assert_no_transaction()
    with tenant_context(tenant_id):
        connection_id = FirmProviderCall.objects.values_list(
            "connection_id", flat=True
        ).get(pk=call_id)
        tenant, connection = _lock(tenant_id, connection_id)
        call = (
            FirmProviderCall.objects.select_for_update(skip_locked=True)
            .filter(pk=call_id)
            .first()
        )
        if call is None:
            return None
        now = timezone.now()
        if call.state in {CallState.CLAIMED, CallState.STARTED}:
            if call.lease_until is not None and call.lease_until <= now:
                _uncertain(call)
            return None
        if call.state not in {CallState.RESERVED, CallState.RETRY}:
            return None
        if call.state == CallState.RETRY and (
            call.attempts != 1 or not prior_401(call)
        ):
            return None
        operation = _operation(call.operation)
        _admit(call, tenant, connection, operation)
        if call.state == CallState.RETRY and isinstance(
            operation.policy, FirmBudgetPolicy
        ):
            operation.policy.reserve(call)
        call.fence += 1
        call.lease_owner = uuid4()
        call.lease_until = now + LEASE
        call.execution_epoch = current_epoch()
        call.state = CallState.CLAIMED
        call.dispatch_started_at = None
        call.finalized_at = None
        call.version += 1
        call.updated_at = now
        call.save()
        return FirmClaim(
            tenant_id, call.pk, call.fence, call.lease_owner, call.execution_epoch
        )


def _prepare(
    ticket: FirmClaim,
) -> FirmDispatch | AdmissionDeniedError | StaleClaimError:
    connection_id = FirmProviderCall.objects.values_list(
        "connection_id", flat=True
    ).get(pk=ticket.call_id)
    tenant, connection = _lock(ticket.tenant_id, connection_id)
    call = FirmProviderCall.objects.select_for_update().get(pk=ticket.call_id)
    if (
        call.fence != ticket.fence
        or call.lease_owner != ticket.owner
        or call.execution_epoch != ticket.execution_epoch
        or current_epoch() != ticket.execution_epoch
        or call.state != CallState.CLAIMED
        or call.dispatch_started_at is not None
    ):
        return StaleClaimError()
    if call.lease_until is None or call.lease_until <= timezone.now():
        _uncertain(call)
        return StaleClaimError()
    try:
        _admit(call, tenant, connection, _operation(call.operation))
    except AdmissionDeniedError as error:
        policy = _operation(call.operation).policy
        if isinstance(policy, FirmBudgetPolicy):
            policy.release(call)
        call.state = (
            CallState.PAUSED if str(error) == CallState.PAUSED else CallState.AUTHORITY
        )
        call.version += 1
        call.save()
        return error
    # Policy and lock waits may have outlived the lease.
    if call.lease_until <= timezone.now():
        _uncertain(call)
        return StaleClaimError()
    call.attempts += 1
    call.dispatch_started_at = timezone.now()
    call.x_request_tag = ticket.owner
    call.state = CallState.STARTED
    call.version += 1
    call.save()
    return FirmDispatch(ticket, FirmJournalRecord.started(call))


def firm_begin_io(ticket: FirmClaim) -> FirmDispatch:
    """Authorize once under locks, commit, then durably record the start."""
    assert_no_transaction()
    with tenant_context(ticket.tenant_id):
        prepared = _prepare(ticket)
    if isinstance(prepared, (AdmissionDeniedError, StaleClaimError)):
        raise prepared
    provider_journal.append(prepared.receipt)
    issue_dispatch(prepared)
    return prepared


def firm_record_result(dispatch: FirmDispatch, result: CallResult) -> None:
    """Durably record the reply before the fenced database finalization."""
    assert_no_transaction()
    provider_journal.append(replace(dispatch.receipt, result=result))


def firm_finalize(ticket: FirmClaim, result: CallResult) -> bool:
    """Monotonic fenced finalize; stale or late replies stay append-only receipts."""
    assert_no_transaction()
    with tenant_context(ticket.tenant_id):
        call = FirmProviderCall.objects.select_for_update().get(pk=ticket.call_id)
        now = timezone.now()
        current = (
            call.fence == ticket.fence
            and call.lease_owner == ticket.owner
            and call.execution_epoch == ticket.execution_epoch
            and current_epoch() == ticket.execution_epoch
        )
        late = FirmProviderCallAttempt(
            attempt_id=ticket.owner,
            attempt_number=call.attempts,
            fence=ticket.fence,
            observation="late",
            occurred_at=call.dispatch_started_at or now,
        )
        if not current:
            observe_firm(call, late, result)
            return False
        if call.state != CallState.STARTED:
            if (
                call.dispatch_started_at is not None
                and not FirmProviderCallAttempt.objects.filter(
                    call=call, attempt_id=ticket.owner, observation="result"
                ).exists()
            ):
                observe_firm(call, late, result)
            return False
        if call.lease_until is None or call.lease_until <= now:
            observe_firm(call, late, result)
            _uncertain(call)
            return False
        # A registration is never retried after an ambiguous outcome (INV-R6).
        if result.state == CallState.RETRY:
            result = replace(result, state=CallState.UNCERTAIN)
        attempt = late
        attempt.observation = "result"
        observe_firm(call, attempt, result)
        call.state = result.state
        call.http_status = result.http_status
        call.provider_code = result.provider_code
        call.response_digest = result.response_digest
        call.finalized_at = now
        call.version += 1
        call.updated_at = now
        call.save()
        policy = _operation(call.operation).policy
        if isinstance(policy, FirmBudgetPolicy):
            policy.settle(call)
        return True


def _firm_perform_once(ticket: FirmClaim, adapter: FirmAdapter) -> CallResult:
    """Start once, transmit once, journal the reply, then finalize under the fence."""
    dispatch = firm_begin_io(ticket)
    assert_no_transaction()
    try:
        with dispatch_window(dispatch):
            result = adapter.send(dispatch)
    except (OSError, HTTPException):
        result = CallResult(
            CallState.UNCERTAIN, None, "connection_lost", "desconhecido"
        )
    if result.state == CallState.RETRY:
        result = replace(result, state=CallState.UNCERTAIN)
    firm_record_result(dispatch, result)
    if not firm_finalize(ticket, result):
        return CallResult(CallState.UNCERTAIN, None, "stale_finalize", "desconhecido")
    return result


def firm_perform(ticket: FirmClaim, adapter: FirmAdapter) -> CallResult:
    """One send per claim; only a complete first 401 opens one fresh firm claim."""
    result = _firm_perform_once(ticket, adapter)
    if schedule_firm_retry(ticket, result):
        retry = firm_claim(ticket.tenant_id, ticket.call_id)
        if retry is not None:
            result = _firm_perform_once(retry, adapter)
    with tenant_context(ticket.tenant_id):
        own_firm_result(FirmProviderCall.objects.get(pk=ticket.call_id))
    return result
