"""The beat dead-man's switch: silence must be detectable.

This product's core promise is "never miss day 20". If the beat container is simply
DOWN, nothing raises, nothing is generated, and Sentry sees nothing — because nothing
failed. The absence of work produces no signal at all, which makes it the single
most dangerous failure mode in the whole scheduler.

So the scheduler asserts its own liveness on a timer, and the probe reports 503 when
that assertion goes quiet. A missing heartbeat row is treated as stale rather than as
"not applicable", because "beat has never run" is precisely the case being watched
for.
"""

from datetime import timedelta
from http import HTTPStatus

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.obligations.heartbeat import (
    HEARTBEAT_INTERVAL,
    HEARTBEAT_STALE_AFTER,
    SCHEDULER_HEARTBEAT_NAME,
    record_heartbeat,
    scheduler_is_alive,
)
from apps.obligations.models import SchedulerHeartbeat

pytestmark = pytest.mark.django_db

FRESH = timedelta(minutes=5)
STALE = timedelta(minutes=16)


def age_heartbeat(by: timedelta) -> None:
    """Backdate the newest heartbeat.

    Written with `update` rather than `save` on purpose: `updated_at` is `auto_now`,
    so a save would helpfully reset it to now and the test would assert nothing.
    """
    SchedulerHeartbeat.objects.update(updated_at=timezone.now() - by)


def test_the_switch_is_armed_by_a_migration_not_by_the_first_beat_tick() -> None:
    # Given a freshly migrated database
    # When the heartbeat table is read
    # Then a row already exists. Seeding it is what closes the hole: with no row at
    # all, "beat has never started" and "beat is healthy" would look identical if
    # absence were ever treated as acceptable.
    assert SchedulerHeartbeat.objects.filter(name=SCHEDULER_HEARTBEAT_NAME).exists()


def test_recording_a_heartbeat_is_idempotent_and_moves_the_timestamp() -> None:
    # Given an aged heartbeat
    age_heartbeat(STALE)
    before = SchedulerHeartbeat.objects.get(name=SCHEDULER_HEARTBEAT_NAME).updated_at

    # When beat reports in
    record_heartbeat()

    # Then the timestamp moves forward and no second row appears
    after = SchedulerHeartbeat.objects.get(name=SCHEDULER_HEARTBEAT_NAME).updated_at
    assert after > before
    assert SchedulerHeartbeat.objects.count() == 1


def test_a_five_minute_old_heartbeat_is_alive() -> None:
    # Given a heartbeat from five minutes ago, which is one missed tick on a
    # five-minute schedule and well inside the fifteen-minute window
    age_heartbeat(FRESH)

    # When liveness is evaluated
    # Then the scheduler is considered alive
    assert scheduler_is_alive() is True


def test_a_sixteen_minute_old_heartbeat_is_dead() -> None:
    # Given a heartbeat older than the fifteen-minute threshold
    age_heartbeat(STALE)

    # When liveness is evaluated
    # Then it reports dead. Three consecutive missed ticks is not a slow worker.
    assert scheduler_is_alive() is False


def test_the_threshold_is_three_missed_ticks_not_one() -> None:
    # Given the configured window
    # When it is compared with the beat interval
    # Then it allows two ticks to be missed before alarming. One would make every
    # redeploy and every slow tick page somebody, and an alarm nobody trusts is
    # indistinguishable from no alarm at all.
    assert timedelta(minutes=15) == HEARTBEAT_STALE_AFTER


def test_the_beat_schedule_ticks_at_the_interval_the_threshold_assumes() -> None:
    # Given the configured beat schedule
    from django.conf import settings  # noqa: PLC0415

    schedule = settings.CELERY_BEAT_SCHEDULE["scheduler-heartbeat"]

    # When its period is compared with the module's own interval
    # Then they agree. The settings entry is a literal, because importing this module
    # into settings would load a model before the app registry is ready — so this
    # assertion is what stops the two from drifting apart unnoticed.
    assert schedule["schedule"] == HEARTBEAT_INTERVAL.total_seconds()
    assert schedule["task"] == "apps.obligations.tasks.record_scheduler_heartbeat"
    assert HEARTBEAT_STALE_AFTER == HEARTBEAT_INTERVAL * 3


def test_no_heartbeat_at_all_is_dead_not_unknown() -> None:
    # Given a database in which the heartbeat row has been removed entirely
    SchedulerHeartbeat.objects.all().delete()

    # When liveness is evaluated
    # Then it reports dead. Treating absence as "nothing to say" would make a beat
    # that never started the quietest possible failure.
    assert scheduler_is_alive() is False


def test_healthz_returns_200_while_the_scheduler_is_reporting(client: Client) -> None:
    # Given a heartbeat from five minutes ago
    age_heartbeat(FRESH)

    # When the probe is called
    response = client.get(reverse("healthz"))

    # Then it answers 200 and says so explicitly. The backup and disk keys are asserted
    # here too, rather than being read past with a subset comparison, so that a future
    # signal cannot be added to this body without a test acknowledging it — which is how
    # the disk key came to be listed below. `test_backup_freshness.py` and
    # `test_disk_headroom.py` own those keys' own behaviour.
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "status": "ok",
        "scheduler": "alive",
        "backup": "stale",
        "disk": "unknown",
    }


def test_healthz_returns_503_when_the_scheduler_has_gone_quiet(
    client: Client,
) -> None:
    # Given a heartbeat aged past the threshold, which is what a stopped beat
    # container looks like from the outside
    age_heartbeat(STALE)

    # When the probe is called
    response = client.get(reverse("healthz"))

    # Then it answers 503. Nothing raised, nothing errored, and nothing was
    # generated — the only way to notice is to look for the silence.
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["scheduler"] == "stale"


def test_healthz_stays_200_when_the_database_is_unreachable(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a database that refuses the heartbeat query
    from django.db import OperationalError  # noqa: PLC0415

    from apps.core import views  # noqa: PLC0415

    refused = "connection refused"

    def explode() -> object:
        raise OperationalError(refused)

    # Patched where it is USED, not where it is defined: apps.core.views binds the
    # name at import, so patching the source module would leave the view calling the
    # original and the test would pass while exercising the happy path.
    monkeypatch.setattr(views, "read_heartbeats", explode)

    # When the LIVENESS probe is called
    response = client.get(reverse("healthz"))

    # Then it still answers 200, reporting the scheduler as unknown. This endpoint
    # decides whether to RESTART the web process, and restarting it does not fix a
    # database outage — it just removes the last component still able to serve a
    # cached page. Readiness is a separate concern and a later endpoint.
    assert response.status_code == HTTPStatus.OK
    assert response.json()["scheduler"] == "unknown"
