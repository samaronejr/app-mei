"""Restore every independent attempt before providers may be unpaused."""

from collections import defaultdict
from copy import copy
from dataclasses import replace
from uuid import UUID

from django.conf import settings
from django.core.management.base import CommandError
from django.db import connection

from apps.core.tasks import PlatformTask, assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.integrations import provider_journal
from apps.integrations.call_contracts import (
    CallResult,
    IntentConflictError,
    operation_for,
)
from apps.integrations.call_models import (
    CallState,
    FirmProviderCall,
    FirmProviderCallAttempt,
    ProviderCall,
    ProviderCallAttempt,
    ProviderEffectBarrier,
)
from apps.integrations.call_observations import meter, observe
from apps.integrations.call_state import current_epoch, lock_controls, uncertain
from apps.integrations.firm_calls import observe_firm
from apps.integrations.journal_schema import (
    FirmJournalRecord,
    JournalError,
    JournalRecord,
)
from apps.integrations.models import FirmAuthorization, ProviderConnection
from apps.tenants.models import Tenant


def _attempts[Receipt: (JournalRecord, FirmJournalRecord)](
    records: list[Receipt],
) -> list[Receipt]:
    starts: dict[UUID, Receipt] = {}
    replies: dict[UUID, Receipt] = {}
    for record in records:
        target = starts if record.result is None else replies
        if record.attempt_id in target and target[record.attempt_id] != record:
            raise JournalError
        target[record.attempt_id] = record
    if replies.keys() - starts.keys():
        raise JournalError
    for attempt_id, reply in replies.items():
        if replace(reply, result=None) != starts[attempt_id]:
            raise JournalError
    ordered = sorted(starts.values(), key=lambda row: row.attempt_number)
    if [row.attempt_number for row in ordered] != list(range(1, len(ordered) + 1)):
        raise JournalError
    return [replies.get(row.attempt_id, row) for row in ordered]


def _restore_attempt(call: ProviderCall, record: JournalRecord) -> int:
    """Rebuild one receipt and settle precisely its original transmission."""
    if (
        record.result is None
        and ProviderCallAttempt.objects.filter(
            call=call, attempt_id=record.attempt_id, observation="result"
        ).exists()
    ):
        # A finalized DB receipt without its independent counterpart proves
        # an incomplete object restore, not a new uncertain provider outcome.
        raise JournalError
    if record.operation_class in {"issuance", "registration", "configuration"}:
        barrier, _ = ProviderEffectBarrier.objects.get_or_create(
            tenant_id=record.tenant_id,
            client_id=record.client_id,
            provider=record.provider,
            business_effect_key=record.business_effect_key,
            defaults={
                "request_digest": record.request_digest,
                "call": call,
                "first_start_receipt": replace(record, result=None).path,
            },
        )
        if (
            barrier.request_digest != record.request_digest
            or barrier.call_id != call.pk
        ):
            raise IntentConflictError
    result = record.result or CallResult(
        CallState.UNCERTAIN, None, "journal_start_only", "desconhecido"
    )
    observation = ProviderCallAttempt(
        attempt_id=record.attempt_id,
        attempt_number=record.attempt_number,
        fence=record.fence,
        occurred_at=record.occurred_at,
        reserved_microbrl=record.reserved_microbrl,
        observation="result" if record.result is not None else "uncertain",
    )
    created = observe(call, observation, result)
    # Hooks see this attempt's liability, not the last attempt's aggregate.
    attempt_call = copy(call)
    attempt_call.reserved_microbrl = record.reserved_microbrl
    attempt_call.tariff_revision = record.tariff_revision
    attempt_call.attempts = record.attempt_number
    attempt_call.fence = record.fence
    attempt_call.execution_epoch = record.execution_epoch
    attempt_call.lease_owner = record.attempt_id
    attempt_call.x_request_tag = record.attempt_id
    attempt_call.dispatch_started_at = record.occurred_at
    if created:
        operation_for(record.operation).policy.settle(attempt_call, result)
    meter(call, record.attempt_id, result, record.occurred_at)
    return int(created)


def _restore(records: list[JournalRecord]) -> int:
    attempts = _attempts(records)
    first = attempts[0]
    _, link = lock_controls(first.tenant_id, first.link_id)
    if link.client_id != first.client_id or link.connection.provider != first.provider:
        raise JournalError
    call, _ = ProviderCall.objects.get_or_create(
        tenant_id=first.tenant_id,
        client_id=first.client_id,
        request_key=first.request_key,
        defaults={
            "id": first.call_id,
            "link": link,
            "operation": first.operation,
            "operation_revision": first.operation_revision,
            "operation_class": first.operation_class,
            "provider": first.provider,
            "environment": first.environment,
            "request_digest": first.request_digest,
            "business_effect_key": first.business_effect_key,
            "billing_class": first.billing_class,
            "tariff_revision": first.tariff_revision,
        },
    )
    call = ProviderCall.objects.select_for_update().get(pk=call.pk)
    if (
        call.pk != first.call_id
        or call.request_digest != first.request_digest
        or call.link_id != first.link_id
        or call.operation != first.operation
        or call.operation_revision != first.operation_revision
        or call.operation_class != first.operation_class
        or call.environment != first.environment
        or call.billing_class != first.billing_class
        or call.business_effect_key != first.business_effect_key
    ):
        raise IntentConflictError
    count = 0
    for record in attempts:
        if (
            record.request_digest != first.request_digest
            or record.request_key != first.request_key
            or record.link_id != first.link_id
            or record.client_id != first.client_id
            or record.business_effect_key != first.business_effect_key
            or record.operation != first.operation
            or record.operation_revision != first.operation_revision
            or record.operation_class != first.operation_class
            or record.billing_class != first.billing_class
            or record.provider != first.provider
            or record.environment != first.environment
        ):
            raise JournalError
        count += _restore_attempt(call, record)
    latest = attempts[-1]
    # Human decisions and later DB attempts are never undone by older receipts.
    if call.attempts > latest.attempt_number or call.state in {
        CallState.RECONCILED,
        CallState.CANCELLED,
    }:
        return count
    result = latest.result or CallResult(
        CallState.UNCERTAIN, None, "journal_start_only", "desconhecido"
    )
    call.attempts = latest.attempt_number
    call.fence = max(call.fence, latest.fence)
    call.execution_epoch = latest.execution_epoch
    call.lease_owner = latest.attempt_id
    call.x_request_tag = latest.attempt_id
    call.reserved_microbrl = latest.reserved_microbrl
    call.tariff_revision = latest.tariff_revision
    call.dispatch_started_at = latest.occurred_at
    call.finalized_at = latest.occurred_at if latest.result is not None else None
    # A scheduled read retry is intentionally quarantined after restore, too:
    # no replay may itself authorize a new paid transmission.
    call.state = (
        CallState.UNCERTAIN if result.state == CallState.RETRY else result.state
    )
    call.http_status = result.http_status
    call.provider_code = result.provider_code
    call.billed = result.billed
    call.response_digest = result.response_digest
    call.settled_microbrl = result.settled_microbrl
    call.version += 1
    call.save()
    if call.state == CallState.UNCERTAIN:
        uncertain(call)
    return count


def _restore_firm(records: list[FirmJournalRecord]) -> int:
    """Rebuild one SP-TERMO submission and every attempt; never send a request."""
    attempts = _attempts(records)
    first = attempts[0]
    # PLATFORM_QUERY_OK: lock exactly the journaled tenant, as dispatch does.
    Tenant.objects.select_for_update(no_key=True).get(pk=first.tenant_id)
    provider = ProviderConnection.objects.select_for_update().get(
        pk=first.connection_id
    )
    authorization = FirmAuthorization.objects.get(pk=first.authorization_id)
    if provider.provider != first.provider:
        raise JournalError
    if (
        FirmProviderCall.objects.filter(authorization=authorization)
        .exclude(request_key=first.request_key)
        .exists()
    ):
        raise IntentConflictError
    call, _ = FirmProviderCall.objects.get_or_create(
        tenant_id=first.tenant_id,
        request_key=first.request_key,
        defaults={
            "id": first.submission_id,
            "authorization": authorization,
            "connection": provider,
            "operation": first.operation,
            "operation_revision": first.operation_revision,
            "operation_class": first.operation_class,
            "provider": first.provider,
            "environment": first.environment,
            "request_digest": first.request_digest,
        },
    )
    call = FirmProviderCall.objects.select_for_update().get(pk=call.pk)
    if (
        call.pk != first.submission_id
        or call.request_digest != first.request_digest
        or call.authorization_id != first.authorization_id
        or call.connection_id != first.connection_id
        or call.operation != first.operation
        or call.operation_revision != first.operation_revision
        or call.environment != first.environment
    ):
        raise IntentConflictError
    count = 0
    for record in attempts:
        if replace(
            record,
            attempt_id=first.attempt_id,
            attempt_number=1,
            fence=first.fence,
            execution_epoch=first.execution_epoch,
            occurred_at=first.occurred_at,
            result=None,
        ) != replace(first, result=None):
            raise JournalError
        result = record.result or CallResult(
            CallState.UNCERTAIN, None, "journal_start_only", "desconhecido"
        )
        observation = FirmProviderCallAttempt(
            attempt_id=record.attempt_id,
            attempt_number=record.attempt_number,
            fence=record.fence,
            occurred_at=record.occurred_at,
            observation="result" if record.result is not None else "uncertain",
        )
        count += int(observe_firm(call, observation, result))
    latest = attempts[-1]
    if call.attempts > latest.attempt_number or call.state in {
        CallState.RECONCILED,
        CallState.CANCELLED,
    }:
        return count
    result = latest.result or CallResult(
        CallState.UNCERTAIN, None, "journal_start_only", "desconhecido"
    )
    call.attempts = latest.attempt_number
    call.fence = max(call.fence, latest.fence)
    call.execution_epoch = latest.execution_epoch
    call.lease_owner = latest.attempt_id
    call.x_request_tag = latest.attempt_id
    call.dispatch_started_at = latest.occurred_at
    call.finalized_at = latest.occurred_at if latest.result is not None else None
    # A registration is never retried: a journaled retry hint stays uncertain.
    call.state = (
        CallState.UNCERTAIN if result.state == CallState.RETRY else result.state
    )
    call.http_status = result.http_status
    call.provider_code = result.provider_code
    call.response_digest = result.response_digest
    call.version += 1
    call.save()
    return count


def reconcile(restore_ref: UUID) -> int:
    """Fail closed on any incomplete scope or receipt; never send a request."""
    assert_no_transaction()
    if not settings.PROVIDERS_PAUSED:
        message = "PROVIDERS_PAUSED must be true during provider reconciliation."
        raise CommandError(message)
    # Session lock serializes restore invocations without holding a transaction
    # across object storage. Operators must also stop every dispatch worker.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(240100)")
    try:
        tenants = set(PlatformTask.tenant_ids(include_inactive=True))
        if provider_journal.tenant_ids() - tenants:
            raise JournalError
        records = [record for tid in tenants for record in provider_journal.read(tid)]
        firm_records = [
            record for tid in tenants for record in provider_journal.read_firm(tid)
        ]
        floor = max(
            [
                current_epoch(),
                provider_journal.epoch_floor(),
                *(r.execution_epoch for r in records),
                *(r.execution_epoch for r in firm_records),
            ]
        )
        provider_journal.record_epoch(floor + 1, restore_ref)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT integrations_restore_provider_epoch(%s, %s)",
                [floor, restore_ref],
            )
        groups: dict[tuple[UUID, UUID], list[JournalRecord]] = defaultdict(list)
        for record in records:
            groups[record.tenant_id, record.call_id].append(record)
        restored = 0
        for (tid, _), rows in groups.items():
            with tenant_context(tid):
                restored += _restore(rows)
        firm_groups: dict[tuple[UUID, UUID], list[FirmJournalRecord]] = defaultdict(
            list
        )
        for firm_record in firm_records:
            firm_groups[firm_record.tenant_id, firm_record.submission_id].append(
                firm_record
            )
        for (tid, _), firm_rows in firm_groups.items():
            with tenant_context(tid):
                restored += _restore_firm(firm_rows)
        return restored
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(240100)")
