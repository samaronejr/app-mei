"""Idempotent reservation and exclusive committed provider leases."""

from datetime import timedelta
from uuid import UUID, uuid4

from django.utils import timezone

from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.integrations.call_contracts import (
    MAX_ATTEMPTS,
    CallRequest,
    Claim,
    IntentConflictError,
    operation_for,
)
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_state import admit, current_epoch, lock_controls, uncertain


def reserve(tenant_id: UUID, link_id: UUID, request: CallRequest) -> ProviderCall:
    """Return the identical intent or reserve its maximum liability once."""
    assert_no_transaction()
    operation = operation_for(request.operation)
    with tenant_context(tenant_id):
        tenant, link = lock_controls(tenant_id, link_id)
        existing = (
            ProviderCall.objects.select_for_update()
            .filter(client_id=link.client_id, request_key=request.request_key)
            .first()
        )
        if existing is not None:
            if (
                existing.request_digest != request.request_digest
                or existing.business_effect_key != request.business_effect_key
                or existing.link_id != link.pk
                or existing.operation != operation.code
                or existing.operation_revision != operation.revision
            ):
                raise IntentConflictError
            return existing
        call = ProviderCall(
            tenant_id=tenant_id,
            client_id=link.client_id,
            link=link,
            operation=operation.code,
            operation_revision=operation.revision,
            operation_class=operation.operation_class,
            provider=link.connection.provider,
            environment=link.connection.environment,
            request_key=request.request_key,
            request_digest=request.request_digest,
            business_effect_key=request.business_effect_key,
            billing_class=operation.billing_class,
            connection_version=link.connection.version,
            link_version=link.version,
            credential_generation=link.credential_generation,
        )
        admit(call, tenant, operation)
        if operation.journaled:
            reservation = operation.policy.reserve(call)
            call.reserved_microbrl = reservation.microbrl
            call.tariff_revision = reservation.tariff_revision
        call.state = CallState.RESERVED
        call.save()
        return call


def claim(tenant_id: UUID, call_id: UUID) -> Claim | None:
    """Commit one lease; an expired in-flight lease is never another send slot."""
    assert_no_transaction()
    with tenant_context(tenant_id):
        link_id = ProviderCall.objects.values_list("link_id", flat=True).get(pk=call_id)
        tenant, link = lock_controls(tenant_id, link_id)
        call = (
            ProviderCall.objects.select_for_update(skip_locked=True)
            .filter(pk=call_id)
            .first()
        )
        if call is None:
            return None
        call.link = link
        now = timezone.now()
        if call.state in {CallState.CLAIMED, CallState.STARTED}:
            if call.lease_until is not None and call.lease_until <= now:
                uncertain(call)
            return None
        if (
            call.state not in {CallState.RESERVED, CallState.RETRY}
            or call.next_attempt_at > now
            or call.attempts >= MAX_ATTEMPTS
        ):
            return None
        operation = operation_for(call.operation)
        admit(call, tenant, operation)
        if call.state == CallState.RETRY and operation.journaled:
            reservation = operation.policy.reserve(call)
            call.reserved_microbrl = reservation.microbrl
            call.tariff_revision = reservation.tariff_revision
        call.fence += 1
        call.lease_owner = uuid4()
        call.lease_until = now + timedelta(seconds=120)
        call.execution_epoch = current_epoch()
        call.state = CallState.CLAIMED
        call.dispatch_started_at = None
        call.finalized_at = None
        call.version += 1
        call.save()
        return Claim(
            tenant_id,
            call.client_id,
            call.pk,
            call.fence,
            call.lease_owner,
            call.execution_epoch,
        )
