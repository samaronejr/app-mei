"""Minute dispatch: collect tenants, commit claims, send, fenced finalization."""

import hashlib
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

import sentry_sdk
from celery import shared_task
from django.utils import timezone

from apps.core.tasks import PhasedTenantTask, PlatformTask, assert_no_transaction
from apps.core.tenancy import MissingTenantContext
from apps.delivery.errors import ReminderSweepError
from apps.delivery.gateway import send
from apps.delivery.policy import enabled_kinds
from apps.delivery.reminder_cancellation import cancel_obsolete_reminders
from apps.delivery.reminders import (
    COUNTERS,
    BatchSelected,
    SweepCounts,
    empty_counts,
    sweep_tenant,
)
from apps.delivery.services import claim_batch, finalize
from apps.obligations.models import SchedulerHeartbeat


@shared_task(base=PhasedTenantTask)
def dispatch_tenant(
    tenant_id: UUID | str, *, before_network_io: Callable[[], None] | None = None
) -> int:
    """No outer transaction; injected hook observes the actual egress boundary."""
    claims = claim_batch(UUID(str(tenant_id)))
    for claim in claims:
        reply = send(claim, before_network_io=before_network_io)
        finalize(claim, reply)
    return len(claims)


@shared_task(base=PlatformTask, bind=True)
def dispatch_outbox(self: PlatformTask) -> int:
    """Propagate failed phases; never advertise partial success with a heartbeat."""
    assert_no_transaction()
    count = 0
    for tid in self.tenant_ids():
        cancel_obsolete_reminders(tid)
        if enabled_kinds(tid):
            count += dispatch_tenant(str(tid))
    SchedulerHeartbeat.objects.update_or_create(name="dispatch_outbox", defaults={})
    return count


@shared_task(base=PhasedTenantTask)
def sweep_tenant_reminders(
    tenant_id: UUID | str,
    *,
    clock: Callable[[], datetime] | None = None,
    after_batch_selected: BatchSelected | None = None,
) -> SweepCounts:
    """Enqueue under the trusted system rule only; dispatch owns every I/O phase."""
    tid = UUID(str(tenant_id))
    try:
        return sweep_tenant(
            tid,
            clock=clock,
            after_batch_selected=after_batch_selected,
        )
    except Exception as error:
        # This task boundary records every unexpected failure and immediately
        # re-raises a typed failure. The platform can then visit the other firms.
        with sentry_sdk.new_scope() as scope:
            scope.set_tag(
                "tenant_id_hash", hashlib.sha256(str(tid).encode()).hexdigest()[:16]
            )
            scope.capture_exception(error)
        msg = "Reminder tenant phase failed."
        raise ReminderSweepError(msg) from error


@shared_task(base=PlatformTask, bind=True)
def sweep_reminders(
    self: PlatformTask,
    *,
    clock: Callable[[], datetime] | None = None,
    after_batch_selected: BatchSelected | None = None,
) -> SweepCounts:
    """Visit each enabled firm fairly; a failed turn cannot advertise completion."""
    assert_no_transaction()
    clock = clock or timezone.now
    counts = empty_counts()
    SchedulerHeartbeat.objects.update_or_create(
        name="sweep_reminders",
        defaults={
            "last_attempt_at": clock(),
            "last_error_code": "reminder_sweep_running",
            "outcomes_json": counts,
        },
    )
    failures: list[Exception] = []
    for tid in sorted(self.tenant_ids()):
        try:
            result = sweep_tenant_reminders(
                str(tid), clock=clock, after_batch_selected=after_batch_selected
            )
        except (ReminderSweepError, MissingTenantContext) as error:
            failures.append(error)
            counts["failed_tenants"] += 1
            # The phased base can reject a now-inactive tenant before the body
            # executes. That refusal still marks this run failed, never complete.
            if isinstance(error, MissingTenantContext):
                with sentry_sdk.new_scope() as scope:
                    scope.set_tag(
                        "tenant_id_hash",
                        hashlib.sha256(str(tid).encode()).hexdigest()[:16],
                    )
                    scope.capture_exception(error)
        else:
            for key in COUNTERS:
                counts[key] += result[key]
    SchedulerHeartbeat.objects.update_or_create(
        name="sweep_reminders",
        defaults={
            "last_error_code": "reminder_sweep_failed" if failures else "",
            "outcomes_json": counts,
            **({} if failures else {"last_success_at": clock()}),
        },
    )
    if failures:
        msg = f"Reminder sweep failed for {len(failures)} tenant(s)."
        raise RuntimeError(msg) from failures[0]
    return counts
