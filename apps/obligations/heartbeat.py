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
from enum import StrEnum

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

# 12 GiB, derived rather than chosen. The deploy job refuses to land an image pair below
# 3 GiB free (`MIN_FREE_KIB` in .github/workflows/ci.yml), and the WAL archive accrues
# 288 x 16 MiB = 4.5 GiB a night because `archive_timeout=300` forces a segment switch
# every five minutes whether or not the segment holds anything. 3 + 2 x 4.5 is therefore
# the smallest floor that fires while deploys still work AND leaves two nightly reports
# to act on — lead time is counted in reports, because ops/backup.sh refreshes this
# number once a night and never in between.
#
# ABSOLUTE, not a percentage. WAL accrues at a fixed rate regardless of disk size, so
# the question an operator needs answered — how many nights do I have — is free / rate.
# A percentage floor would silently shorten that on a smaller disk and lengthen it on a
# larger one, which is backwards.
MIN_FREE_DISK_KIB = 12 * 1024 * 1024


class DiskHeadroom(StrEnum):
    """How much room the box had left when it last looked."""

    OK = "ok"
    LOW = "low"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class HeartbeatReport:
    """Every operational signal this deployment reports, read together."""

    scheduler_alive: bool
    backup_fresh: bool
    disk: DiskHeadroom


def _is_within(stamp: datetime | None, window: timedelta, now: datetime) -> bool:
    if stamp is None:
        return False
    return now - stamp <= window


def _headroom(free_kib: int | None, *, measurement_is_fresh: bool) -> DiskHeadroom:
    if free_kib is None:
        return DiskHeadroom.UNKNOWN
    if free_kib < MIN_FREE_DISK_KIB:
        # Reported regardless of the measurement's age. Free space only moves one way
        # between runs, so an old low reading is at worst an understatement, and
        # discarding it would throw away a true alarm.
        return DiskHeadroom.LOW
    # A stale "ok" is the one answer this must never give. The WAL prune lives inside
    # the nightly job, so the disk fills FASTEST in exactly the window where the marker
    # has stopped being refreshed — reporting yesterday's comfort there would be the
    # fuse lying at the only moment it is load-bearing.
    return DiskHeadroom.OK if measurement_is_fresh else DiskHeadroom.UNKNOWN


def read_heartbeats() -> HeartbeatReport:
    """Report both switches from ONE query.

    `/healthz` is hit by the container healthcheck every ten seconds on a box with two
    gunicorn workers and twenty-four connection slots, so the probe's query budget is a
    real constraint rather than a stylistic one. Both signals live in one table keyed by
    name, which is what makes a single filtered read enough.
    """
    rows: dict[str, tuple[datetime, int | None]] = {
        name: (updated_at, free_disk_kib)
        for name, updated_at, free_disk_kib in SchedulerHeartbeat.objects.filter(
            name__in=(SCHEDULER_HEARTBEAT_NAME, BACKUP_HEARTBEAT_NAME),
        ).values_list("name", "updated_at", "free_disk_kib")
    }
    now = timezone.now()
    scheduler = rows.get(SCHEDULER_HEARTBEAT_NAME)
    backup = rows.get(BACKUP_HEARTBEAT_NAME)
    backup_fresh = _is_within(backup[0] if backup else None, BACKUP_STALE_AFTER, now)
    return HeartbeatReport(
        scheduler_alive=_is_within(
            scheduler[0] if scheduler else None,
            HEARTBEAT_STALE_AFTER,
            now,
        ),
        backup_fresh=backup_fresh,
        disk=_headroom(
            backup[1] if backup else None,
            measurement_is_fresh=backup_fresh,
        ),
    )


def backup_is_fresh() -> bool:
    """Report whether a complete backup finished recently enough.

    A missing row answers False for the same reason the scheduler's does: a deployment
    that has never completed a backup is unprotected, which is the loudest thing this
    switch has to say, not a case it has no opinion about.
    """
    return read_heartbeats().backup_fresh


def disk_headroom() -> DiskHeadroom:
    """Report how much room the box had left when the nightly job last measured it.

    Only `ops/backup.sh` writes this number, because it is the one part of this system
    that runs on the host rather than in a container and can therefore see the real
    filesystem. The consequence is that the reading is up to twenty-four hours old, and
    the WAL archive grows about 4.5 GiB a night — so this answers "was there room last
    night", never "is there room now". That is still several nights of warning before a
    disk this size collides, which is the entire point: the condition it watches for
    would otherwise arrive with no warning at all.
    """
    return read_heartbeats().disk


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
