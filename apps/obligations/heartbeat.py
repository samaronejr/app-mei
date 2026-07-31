"""The scheduler's dead-man's switch.

The beat process fails silently by construction. When it stops, no task raises, no
obligation is generated, and nothing reaches Sentry — because nothing failed. There
is no error to catch and no exception to report; there is only an absence of work,
and absence produces no signal.

So the scheduler is required to assert its own liveness on a timer, and the liveness
probe alarms on the silence. The threshold is deliberately three beat intervals: one
would page somebody on every redeploy and every slow tick, and an alarm nobody trusts
is indistinguishable from no alarm at all.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from apps.obligations.models import SchedulerHeartbeat

SCHEDULER_HEARTBEAT_NAME = "beat"
HEARTBEAT_INTERVAL = timedelta(minutes=5)
HEARTBEAT_STALE_AFTER = timedelta(minutes=15)

BACKUP_HEARTBEAT_NAME = "backup"
BACKUP_INTERVAL = timedelta(days=1)
# One late run must never alarm and one skipped night always must, which puts the
# threshold strictly between one interval and two. The scheduler's "three missed ticks"
# rule cannot be borrowed: at one tick per day it would mean three days of silence, and
# the 2026-07-29 outage — the reason this signal exists — lasted two.
BACKUP_STALE_AFTER = timedelta(hours=36)


@dataclass(frozen=True, slots=True)
class HeartbeatReport:
    """Both operational dead-man's switches, read together."""

    scheduler_alive: bool
    backup_fresh: bool


def _is_within(stamp: datetime | None, window: timedelta, now: datetime) -> bool:
    if stamp is None:
        return False
    return now - stamp <= window


def read_heartbeats() -> HeartbeatReport:
    """Report both switches from ONE query.

    `/healthz` is hit by the container healthcheck every ten seconds on a box with two
    gunicorn workers and twenty-four connection slots, so the probe's query budget is a
    real constraint rather than a stylistic one. Both signals live in one table keyed by
    name, which is what makes a single filtered read enough.
    """
    stamps: dict[str, datetime] = dict(
        SchedulerHeartbeat.objects.filter(
            name__in=(SCHEDULER_HEARTBEAT_NAME, BACKUP_HEARTBEAT_NAME),
        ).values_list("name", "updated_at"),
    )
    now = timezone.now()
    return HeartbeatReport(
        scheduler_alive=_is_within(
            stamps.get(SCHEDULER_HEARTBEAT_NAME),
            HEARTBEAT_STALE_AFTER,
            now,
        ),
        backup_fresh=_is_within(
            stamps.get(BACKUP_HEARTBEAT_NAME),
            BACKUP_STALE_AFTER,
            now,
        ),
    )


def backup_is_fresh() -> bool:
    """Report whether a complete backup finished recently enough.

    A missing row answers False for the same reason the scheduler's does: a deployment
    that has never completed a backup is unprotected, which is the loudest thing this
    switch has to say, not a case it has no opinion about.
    """
    return read_heartbeats().backup_fresh


def record_heartbeat() -> None:
    """Assert that the scheduler is still running.

    `update_or_create` keyed on the name, so redeploys and multiple beat restarts
    move one row rather than accumulating rows nobody reads. `updated_at` is
    `auto_now`, so saving is what stamps it.
    """
    heartbeat, created = SchedulerHeartbeat.objects.get_or_create(
        name=SCHEDULER_HEARTBEAT_NAME,
    )
    if not created:
        heartbeat.save(update_fields=["updated_at"])


def scheduler_is_alive() -> bool:
    """Report whether the scheduler has checked in recently enough.

    A missing row answers False rather than True. "Beat has never started" is
    precisely the condition being watched for, so treating absence as acceptable
    would make the switch mute in the one case it exists to catch.

    Keyed by NAME, not by recency. This read used to take the newest row in the table,
    which was indistinguishable from correct while beat was the only writer — and stops
    being correct the moment a second signal shares the table, because a nightly backup
    would then answer on beat's behalf and silence this alarm until morning.
    """
    return read_heartbeats().scheduler_alive
