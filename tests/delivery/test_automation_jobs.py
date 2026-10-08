"""Schedule estimates follow beat's timezone and persisted registration."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from celery.schedules import crontab
from django.apps import apps
from django.conf import settings as project_settings
from pytest_django.fixtures import SettingsWrapper

from apps.delivery.jobs import JOB_LABELS, registered_jobs
from apps.obligations.models import SchedulerHeartbeat

pytestmark = pytest.mark.django_db(transaction=True)
NOW = datetime(2026, 10, 7, 5, tzinfo=UTC)


def test_crontab_estimate_uses_brazilian_scheduler_time(
    settings: SettingsWrapper,
) -> None:
    # Given a 06:00 BRT job, while the clock is 02:00 BRT.
    settings.CELERY_BEAT_SCHEDULE = {
        "brazil-six": {
            "task": "apps.cycles.tasks.open_deterministic_exceptions",
            "schedule": crontab(hour=6, minute=0),
        }
    }

    # When the page calculates its next execution.
    with patch("apps.delivery.jobs.timezone.now", return_value=NOW):
        job = registered_jobs()[0]

    # Then it is 09:00 UTC, not the incorrect 06:00 UTC seen in the first live QA.
    assert job.next_run_at == datetime(2026, 10, 7, 9, tzinfo=UTC)


def test_persisted_interval_uses_dispatch_time_but_completion_stays_distinct(
    settings: SettingsWrapper,
) -> None:
    # Given a DB override with a recent dispatch and an older completion.
    settings.CELERY_BEAT_SCHEDULE = {
        "dispatch-outbox": {
            "task": "apps.delivery.tasks.dispatch_outbox",
            "schedule": 60.0,
        }
    }
    interval = apps.get_model("django_celery_beat", "IntervalSchedule").objects.create(
        every=43, period="seconds"
    )
    apps.get_model("django_celery_beat", "PeriodicTask").objects.create(
        name="dispatch-outbox",
        task="apps.delivery.tasks.dispatch_outbox",
        interval=interval,
        last_run_at=NOW - timedelta(seconds=20),
    )
    SchedulerHeartbeat.objects.create(name="dispatch_outbox")
    SchedulerHeartbeat.objects.filter(name="dispatch_outbox").update(
        updated_at=NOW - timedelta(minutes=3)
    )

    # When the page reads the effective registration.
    with patch("apps.delivery.jobs.timezone.now", return_value=NOW):
        job = registered_jobs()[0]

    # Then the schedule override wins without claiming dispatch was completion.
    assert job.next_run_at == NOW + timedelta(seconds=23)
    assert job.last_completed_at == NOW - timedelta(minutes=3)


def test_disabled_database_job_has_no_next_run(settings: SettingsWrapper) -> None:
    # Given a registered job disabled in the database scheduler.
    settings.CELERY_BEAT_SCHEDULE = {
        "dispatch-outbox": {
            "task": "apps.delivery.tasks.dispatch_outbox",
            "schedule": 60.0,
        }
    }
    interval = apps.get_model("django_celery_beat", "IntervalSchedule").objects.create(
        every=60, period="seconds"
    )
    apps.get_model("django_celery_beat", "PeriodicTask").objects.create(
        name="dispatch-outbox",
        task="apps.delivery.tasks.dispatch_outbox",
        interval=interval,
        enabled=False,
    )

    # When its status is projected.
    job = registered_jobs()[0]

    # Then no guessed next run is offered for a disabled task.
    assert not job.enabled
    assert job.next_run_at is None
    assert job.last_completed_at is None


def test_newly_registered_job_appears_without_a_hard_coded_inventory(
    settings: SettingsWrapper,
) -> None:
    # Given a future registration, absent from this version's presentation metadata.
    settings.CELERY_BEAT_SCHEDULE = {
        "future-registered-job": {
            "task": "example.tasks.future_registered_job",
            "schedule": 73.0,
        }
    }

    # When the page reads the registration.
    with patch("apps.delivery.jobs.timezone.now", return_value=NOW):
        jobs = registered_jobs()

    # Then it is represented with an honest missing heartbeat and estimated cadence.
    assert len(jobs) == 1
    assert jobs[0].name == "future-registered-job"
    assert jobs[0].last_completed_at is None
    assert jobs[0].next_run_at == NOW + timedelta(seconds=73)


def test_every_shipped_job_has_a_portuguese_label() -> None:
    # Given every task the product actually registers with beat.
    tasks = {
        definition["task"].rsplit(".", 1)[-1]
        for definition in project_settings.CELERY_BEAT_SCHEDULE.values()
    }

    # Then the Automações page names each one; the identifier fallback is only for
    # registrations this version does not ship.
    assert sorted(tasks - JOB_LABELS.keys()) == []
