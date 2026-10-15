"""Bounded tenant turns enqueue existing reminders, never perform external I/O."""

from collections.abc import Callable
from datetime import date, datetime
from typing import Final, Literal, TypedDict
from uuid import UUID

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Exists, OuterRef, Q
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.flags import tenant_row_has
from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import CLIENT_OPEN_STATES, EvidenceRequest, counted_links
from apps.cycles.services.cycle import OPEN_STATES
from apps.cycles.services.requests import recipients
from apps.delivery.errors import IntentConflict
from apps.delivery.models import OutboundMessage, TenantNoticePolicy
from apps.delivery.notice_authority import (
    NoticeOrigin,
    NoticeRefusalError,
    granted_automatic_reminders,
    lock_notice_scope,
)
from apps.delivery.outbox import enqueue, idempotency_digest
from apps.delivery.reminder_cancellation import cancel_obsolete_reminders
from apps.delivery.reminder_rule import (
    ReminderOccurrence,
    daily_reminder_key,
    occurrence_day,
    reminder_intent,
)
from apps.delivery.services import _control
from apps.tenants.account_state import read_only_reason
from apps.tenants.models import Tenant

TENANT_REQUEST_LIMIT: Final = 50
REQUEST_RECIPIENT_LIMIT: Final = 50
type CounterName = Literal[
    "evaluated",
    "queued",
    "existing",
    "skipped",
    "conflicts",
    "cancelled",
    "failed_tenants",
]
COUNTERS: Final[tuple[CounterName, ...]] = (
    "evaluated",
    "queued",
    "existing",
    "skipped",
    "conflicts",
    "cancelled",
    "failed_tenants",
)
type BatchSelected = Callable[[UUID, tuple[UUID, ...]], None]


class SweepCounts(TypedDict):
    """Only completed tenant phases contribute counts; failures are explicit."""

    evaluated: int
    queued: int
    existing: int
    skipped: int
    conflicts: int
    cancelled: int
    failed_tenants: int


def empty_counts() -> SweepCounts:
    """Return a fresh accumulator owned by one invocation, never shared."""
    return {
        "evaluated": 0,
        "queued": 0,
        "existing": 0,
        "skipped": 0,
        "conflicts": 0,
        "cancelled": 0,
        "failed_tenants": 0,
    }


def _select_batch(tid: UUID, now: datetime) -> tuple[UUID, ...]:
    """Choose a bounded cursor turn before any observer or source transaction."""
    with tenant_context(tid):
        # PLATFORM_QUERY_OK: one explicit phased task, not tenant data enumeration.
        tenant = Tenant.objects.get(pk=tid, is_active=True)
        if (
            settings.DELIVERY_PAUSED
            or not tenant_row_has(tenant, "cycles_r1")
            or read_only_reason(tenant, on=occurrence_day(now))
            or not granted_automatic_reminders(
                TenantNoticePolicy.objects.filter().first()
            )
        ):
            return ()
        control = _control()
        if control.paused:
            return ()
        candidates = (
            EvidenceRequest.objects.filter(
                state__in=CLIENT_OPEN_STATES,
                due_on__lt=occurrence_day(now),
                requested_at__lte=now,
                cycle__state__in=OPEN_STATES,
            )
            .annotate(
                submitted=Exists(counted_links().filter(request_id=OuterRef("pk")))
            )
            .filter(submitted=False)
            .order_by("pk")
        )
        cursor = control.reminder_cursor
        partial = control.reminder_contact_cursor is not None
        tail = (
            candidates.filter(pk__gte=cursor)
            if partial and cursor is not None
            else candidates.filter(pk__gt=cursor)
            if cursor is not None
            else candidates
        )
        selected = list(tail.values_list("pk", flat=True)[:TENANT_REQUEST_LIMIT])
        if cursor is not None and len(selected) < TENANT_REQUEST_LIMIT:
            selected.extend(
                (
                    candidates.filter(pk__lt=cursor)
                    if partial
                    else candidates.filter(pk__lte=cursor)
                ).values_list("pk", flat=True)[: TENANT_REQUEST_LIMIT - len(selected)]
            )
        return tuple(selected)


def _advance_request(tid: UUID, request_id: UUID) -> None:
    """Move past a refused request so an old contact cursor never blocks the firm."""
    with tenant_context(tid):
        control = _control()
        control.reminder_cursor = request_id
        control.reminder_contact_cursor = None
        control.save(update_fields=["reminder_cursor", "reminder_contact_cursor"])


def _queue_request(
    tid: UUID,
    request_id: UUID,
    on: date,
    clock: Callable[[], datetime],
) -> tuple[int, int, bool]:
    """Control -> full owner/policy suffix -> one request -> new outbox rows."""
    with tenant_context(tid):
        control = _control()
        if settings.DELIVERY_PAUSED or control.paused:
            return 0, 0, True
        request = EvidenceRequest.objects.get(pk=request_id)
        policy = TenantNoticePolicy.objects.filter().first()
        if policy is None or policy.acknowledged_by_id is None:
            msg = "notice_policy_unacknowledged"
            raise NoticeRefusalError(msg)
        # Discovery is not authority. lock_notice_scope reloads and locks the union
        # before Tenant/policy/source rows; this is attribution, not an owner session.
        # PLATFORM_QUERY_OK: only the candidate policy authorizer is looked up.
        authorizer = User.objects.get(pk=policy.acknowledged_by_id)
        scope = lock_notice_scope(
            authorizer,
            NoticeOrigin(
                "request_reminder", request.client_id, request.cycle_id, request.pk
            ),
            reminder=ReminderOccurrence(on, clock),
        )
        cycle = scope.cycle
        if cycle is None:
            msg = "notice_source_changed"
            raise NoticeRefusalError(msg)
        queued = existing = 0
        cursor = (
            control.reminder_contact_cursor
            if control.reminder_cursor == request_id
            else None
        )
        audience = sorted(
            (
                row
                for row in recipients(scope.client)
                if row.eligible and (cursor is None or row.contact.pk > cursor)
            ),
            key=lambda row: row.contact.pk,
        )
        selected = audience[:REQUEST_RECIPIENT_LIMIT]
        for recipient in selected:
            intent = reminder_intent(cycle, request.pk, recipient.contact.pk, on)
            daily = daily_reminder_key(request.pk, recipient.contact.pk, on)
            previous = OutboundMessage.objects.filter(
                Q(business_intent_key=intent.key(tid))
                | Q(idempotency_key=idempotency_digest(daily)),
                client=scope.client,
            ).exists()
            # A differently bound earlier message for this day raises IntentConflict.
            enqueue(
                scope.actor,
                scope.client.pk,
                recipient.contact.pk,
                intent=intent,
                expected_version=scope.client.version,
                kind="request_reminder",
                cycle_id=cycle.pk,
                invite=recipient.invite,
                evidence_request=request,
                notice_scope=scope,
                idempotency_key=daily,
            )
            if previous:
                existing += 1
            else:
                queued += 1
        complete = len(audience) <= REQUEST_RECIPIENT_LIMIT
        control.reminder_cursor = request_id
        control.reminder_contact_cursor = None if complete else selected[-1].contact.pk
        control.save(update_fields=["reminder_cursor", "reminder_contact_cursor"])
        return queued, existing, complete


def sweep_tenant(
    tid: UUID,
    *,
    clock: Callable[[], datetime] | None = None,
    after_batch_selected: BatchSelected | None = None,
) -> SweepCounts:
    """Run one bounded tenant turn with no outer transaction or transport callback."""
    assert_no_transaction()
    clock = clock or timezone.now
    counts = empty_counts()
    counts["cancelled"] = cancel_obsolete_reminders(tid, clock=clock)
    now = clock()
    selected = _select_batch(tid, now)
    if selected and after_batch_selected is not None:
        after_batch_selected(tid, selected)
    for request_id in selected:
        counts["evaluated"] += 1
        try:
            queued, existing, complete = _queue_request(
                tid, request_id, occurrence_day(now), clock
            )
        except IntentConflict:
            # Another immutable authority/content won this occurrence. Never rebind.
            counts["conflicts"] += 1
            _advance_request(tid, request_id)
        except (
            PermissionDenied,
            ValidationError,
            Http404,
            ClientCompany.DoesNotExist,
            EvidenceRequest.DoesNotExist,
            User.DoesNotExist,
        ):
            counts["skipped"] += 1
            _advance_request(tid, request_id)
        else:
            counts["queued"] += queued
            counts["existing"] += existing
            if not queued and not existing:
                counts["skipped"] += 1
            if not complete:
                break
    return counts
