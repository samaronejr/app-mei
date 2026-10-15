"""A failed sweep is not a completion heartbeat or a successful tenant outcome."""

from datetime import timedelta
from uuid import UUID

import pytest
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.delivery import tasks
from apps.delivery.jobs import registered_jobs
from apps.obligations.models import SchedulerHeartbeat
from tests.delivery import reminder_fixtures
from tests.delivery.reminder_fixtures import (
    NOW,
    allow_automatic,
    overdue_setup,
    reminders,
)
from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)
reminder_setup = reminder_fixtures.reminder_setup


def test_failed_sweep_preserves_last_success_and_is_visible_in_registered_jobs(
    reminder_setup: PortalSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given one real completed run and a subscribed worker selection failure.
    setup = reminder_setup
    allow_automatic(setup)
    completed = tasks.sweep_reminders(clock=lambda: NOW)
    later = NOW + timedelta(minutes=1)
    monkeypatch.setattr(timezone, "now", lambda: later)

    def fail_after_selection(_tenant_id: UUID, _requests: tuple[UUID, ...]) -> None:
        msg = "Injected worker failure after selection"
        raise RuntimeError(msg)

    # When the next real task fails at its exact worker event.
    with pytest.raises(RuntimeError, match="Reminder sweep failed"):
        tasks.sweep_reminders(
            clock=lambda: later, after_batch_selected=fail_after_selection
        )

    # Then the heartbeat reports failure and the last completion remains the old one.
    heartbeat = SchedulerHeartbeat.objects.get(name="sweep_reminders")
    assert heartbeat.last_success_at == NOW
    assert heartbeat.last_attempt_at == later
    assert heartbeat.last_error_code == "reminder_sweep_failed"
    assert heartbeat.outcomes_json["failed_tenants"] == 1
    [job] = [job for job in registered_jobs() if job.name == "sweep-reminders"]
    assert job.last_completed_at == NOW
    assert job.last_error_code == "reminder_sweep_failed"
    assert completed["queued"] == 1


def test_failed_tenant_does_not_starve_another_tenants_batch(
    reminder_setup: PortalSetup,
) -> None:
    # Given two opted-in firms, one of which will fail after selecting its batch.
    failed, healthy = reminder_setup, overdue_setup("reminder-healthy")
    allow_automatic(failed)
    allow_automatic(healthy)

    def fail_one(tenant_id: UUID, _requests: tuple[UUID, ...]) -> None:
        if str(tenant_id) == str(failed.graph.tenant.pk):
            msg = "Injected single-tenant failure"
            raise RuntimeError(msg)

    # When the platform sweep encounters that failure.
    with pytest.raises(RuntimeError, match="Reminder sweep failed"):
        tasks.sweep_reminders(clock=lambda: NOW, after_batch_selected=fail_one)

    # Then the other firm still gets its bounded turn, but the run remains failed.
    assert reminders(failed) == []
    assert len(reminders(healthy)) == 1
    heartbeat = SchedulerHeartbeat.objects.get(name="sweep_reminders")
    assert heartbeat.last_success_at is None
    assert heartbeat.outcomes_json["queued"] == 1
    assert heartbeat.outcomes_json["failed_tenants"] == 1


def test_shipped_beat_schedule_registers_the_sweep(
    settings: SettingsWrapper,
) -> None:
    # Given the shipped, not dynamically invented, job registration.
    definition = settings.CELERY_BEAT_SCHEDULE["sweep-reminders"]

    # Then the existing ops inventory can discover its real task and cadence.
    assert definition["task"] == "apps.delivery.tasks.sweep_reminders"
    assert definition["schedule"] == 300.0


def test_lost_worker_attempt_is_overdue_without_a_fake_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a worker-start heartbeat but no completion or persisted beat dispatch.
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    SchedulerHeartbeat.objects.create(
        name="sweep_reminders",
        last_attempt_at=NOW - timedelta(hours=1),
        last_error_code="reminder_sweep_running",
    )

    # When the ops projection reads the injected current clock.
    [job] = [job for job in registered_jobs() if job.name == "sweep-reminders"]

    # Then a silent worker is visibly overdue, not perpetually five minutes away.
    assert job.last_completed_at is None
    assert job.last_error_code == "reminder_sweep_running"
    assert job.due
    assert job.next_run_at == NOW
