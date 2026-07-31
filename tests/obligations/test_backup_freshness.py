"""The backup dead-man's switch: a nightly job that stops must not stop quietly.

`ops/backup.sh` failed for two days in July 2026 and nobody found out. Nothing raised,
nothing reached Sentry, and the only trace was a non-zero exit code written into a
terminal that had already closed. That is the same failure shape the scheduler switch
exists for — an absence of work, which produces no signal — so it gets the same
mechanism: the job asserts its own success on a timer and the probe reports the silence.

**A stale backup reports 503 nowhere.** The scheduler switch answers 503 because a dead
beat means the application is not doing its job, and an orchestrator restarting the web
process is a reasonable response. A stale backup means the application is perfectly
healthy and an operator must act. Turning that into a 503 would take the site down over
a problem the site does not have, fail the deploy job's fourth assertion, and make the
container healthcheck flap — a strictly worse outage than the one being reported.
"""

from datetime import timedelta
from http import HTTPStatus

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.obligations.heartbeat import (
    BACKUP_HEARTBEAT_NAME,
    BACKUP_INTERVAL,
    BACKUP_STALE_AFTER,
    SCHEDULER_HEARTBEAT_NAME,
    backup_is_fresh,
    scheduler_is_alive,
)
from apps.obligations.models import SchedulerHeartbeat

pytestmark = pytest.mark.django_db

RECENT = timedelta(hours=12)
MISSED_ONE_NIGHT = timedelta(hours=37)


def record_backup(age: timedelta) -> None:
    """Stamp the backup marker as though a run finished `age` ago.

    `updated_at` is `auto_now`, so the backdating has to go through `update` — a save
    would reset the column to now and the test would assert nothing.
    """
    SchedulerHeartbeat.objects.get_or_create(name=BACKUP_HEARTBEAT_NAME)
    SchedulerHeartbeat.objects.filter(name=BACKUP_HEARTBEAT_NAME).update(
        updated_at=timezone.now() - age,
    )


def age_scheduler(by: timedelta) -> None:
    """Backdate the scheduler's own heartbeat, and only that one."""
    SchedulerHeartbeat.objects.filter(name=SCHEDULER_HEARTBEAT_NAME).update(
        updated_at=timezone.now() - by,
    )


def test_a_backup_from_twelve_hours_ago_is_fresh() -> None:
    # Given a backup that finished half a day ago, well inside one nightly cycle
    record_backup(RECENT)

    # When freshness is evaluated
    # Then it reports fresh
    assert backup_is_fresh() is True


def test_a_backup_from_thirty_seven_hours_ago_is_stale() -> None:
    # Given a marker older than the threshold, which is what one skipped night looks
    # like from the outside
    record_backup(MISSED_ONE_NIGHT)

    # When freshness is evaluated
    # Then it reports stale
    assert backup_is_fresh() is False


def test_no_backup_marker_at_all_is_stale_not_unknown() -> None:
    # Given a deployment that has never completed a backup
    assert not SchedulerHeartbeat.objects.filter(name=BACKUP_HEARTBEAT_NAME).exists()

    # When freshness is evaluated
    # Then it reports stale. A deployment with no backup is unprotected, which is the
    # loudest thing this switch has to say — not a case it has no opinion about.
    assert backup_is_fresh() is False


def test_the_threshold_allows_one_late_run_but_not_one_missed_night() -> None:
    # Given the nightly interval and the staleness window
    # When they are compared
    # Then the window is longer than one cycle, so a slow or slightly late run never
    # alarms, and shorter than two, so a single skipped night does. The scheduler's
    # "three missed ticks" rule cannot be copied here: at one tick per day it would
    # mean three days of silence, and the outage this exists to catch lasted two.
    assert timedelta(days=1) == BACKUP_INTERVAL
    assert timedelta(hours=36) == BACKUP_STALE_AFTER
    assert BACKUP_INTERVAL < BACKUP_STALE_AFTER < BACKUP_INTERVAL * 2


def test_a_fresh_backup_cannot_resurrect_a_dead_scheduler() -> None:
    # Given a scheduler that has been silent for well past its threshold, and a backup
    # that completed a moment ago
    age_scheduler(timedelta(minutes=16))
    record_backup(timedelta(minutes=1))

    # When the scheduler's liveness is evaluated
    # Then it is still dead. Both signals share one table keyed by name, so a reader
    # that took "the newest row" would let a nightly backup answer for beat and silence
    # the T-041 alarm for the rest of the night.
    assert scheduler_is_alive() is False


def test_healthz_reports_a_fresh_backup(client: Client) -> None:
    # Given a backup that finished half a day ago
    record_backup(RECENT)

    # When the probe is called
    response = client.get(reverse("healthz"))

    # Then it says so alongside the scheduler signal
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "status": "ok",
        "scheduler": "alive",
        "backup": "fresh",
    }


def test_healthz_reports_a_stale_backup_without_taking_the_site_down(
    client: Client,
) -> None:
    # Given a backup that has not completed for longer than the threshold, while the
    # scheduler is perfectly healthy
    record_backup(MISSED_ONE_NIGHT)

    # When the probe is called
    response = client.get(reverse("healthz"))

    # Then the staleness is visible in the body and the probe still answers 200 with
    # status "ok". The container healthcheck and the deploy job's fourth assertion both
    # key off exactly those two things, so anything else would convert an operator task
    # into an outage and a blocked deploy.
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "status": "ok",
        "scheduler": "alive",
        "backup": "stale",
    }


def test_healthz_still_answers_503_for_the_scheduler_while_a_backup_is_fresh(
    client: Client,
) -> None:
    # Given a dead scheduler and a fresh backup
    age_scheduler(timedelta(minutes=16))
    record_backup(RECENT)

    # When the probe is called
    response = client.get(reverse("healthz"))

    # Then the scheduler switch still fires. Adding a second signal must not soften the
    # first one — this is the same contamination as the direct-read test above, asserted
    # at the surface an orchestrator actually watches.
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {
        "status": "degraded",
        "scheduler": "stale",
        "backup": "fresh",
    }


def test_healthz_reports_backup_unknown_when_the_database_is_unreachable(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a database that refuses the heartbeat query
    from django.db import OperationalError  # noqa: PLC0415

    from apps.core import views  # noqa: PLC0415

    refused = "connection refused"

    def explode() -> object:
        raise OperationalError(refused)

    # Patched where it is USED: apps.core.views binds the name at import, so patching
    # the source module would leave the view calling the original.
    monkeypatch.setattr(views, "read_heartbeats", explode)

    # When the liveness probe is called
    response = client.get(reverse("healthz"))

    # Then both signals report unknown and the probe still answers 200. Restarting the
    # web process fixes neither a database outage nor a missing backup.
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "status": "ok",
        "scheduler": "unknown",
        "backup": "unknown",
    }
