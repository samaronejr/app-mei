"""The scheduler's dead-man's switch.

The beat process fails silently by construction. When it stops, no task raises, no
obligation is generated, and nothing reaches Sentry — because nothing failed. There
is no error to catch and no exception to report; there is only an absence of work,
and absence produces no signal.

So the scheduler is required to assert its own liveness on a timer, and the liveness
probe alarms on the silence. The threshold is deliberately three beat intervals: one
would page somebody on every redeploy and every slow tick, and an alarm nobody trusts
is indistinguishable from no alarm at all.

The free-space reading rides this signal rather than the nightly backup, because a fuse
is only worth what its latency allows: the nightly stamp put eight and a half hours
between crossing the floor and saying so. Riding the heartbeat costs one `statvfs` per
tick and makes the same crossing visible within one tick plus the decay window. It also
means a stalled writer no longer vouches for the number it last wrote — which was
already true of the nightly marker and is preserved deliberately here.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from shutil import disk_usage

from django.utils import timezone

from apps.obligations.models import SchedulerHeartbeat

logger = logging.getLogger(__name__)

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
# the smallest floor that fires while deploys still work AND leaves two nights to act.
#
# The runway is still counted in NIGHTS even though the reading is now minutes-fresh,
# and that is not a leftover. Alert latency and response time are different quantities:
# what the fuse demands is a capacity decision — shorten `RETENTION_DAYS`, lengthen
# `archive_timeout`, or gzip `archive_command`, each of which trades recovery window,
# RPO or restore procedure — and that is days of an operator's attention regardless of
# how promptly the alarm arrives. Making the reading fresh bought a warning that lands
# when the floor is crossed instead of the next morning; it did not make the disk fill
# any slower, so it cannot buy back any of the floor.
#
# ABSOLUTE, not a percentage. WAL accrues at a fixed rate regardless of disk size, so
# the question an operator needs answered — how many nights do I have — is free / rate.
# A percentage floor would silently shorten that on a smaller disk and lengthen it on a
# larger one, which is backwards.
MIN_FREE_DISK_KIB = 12 * 1024 * 1024

# The reading is carried by the scheduler heartbeat, so it decays on the scheduler's
# window rather than the nightly job's. Named separately from HEARTBEAT_STALE_AFTER,
# and asserted equal to it, because the two answer different questions — "is beat
# running" and "is this number still worth printing" — and a future deployment could
# reasonably want to widen one without the other.
DISK_STALE_AFTER = HEARTBEAT_STALE_AFTER

# The container's own root, which is NOT an approximation of the host filesystem. A
# container's `/` is an overlay whose upper layer lives in the daemon's snapshot store,
# and `statvfs` on an overlay is answered by the filesystem backing that layer — so this
# reports the free space of the filesystem where image layers and volumes are actually
# written, which is the quantity the deploy job's MIN_FREE_KIB preflight is also asking
# about. Measuring the container's own writable layer is what makes that true; it is not
# a proxy for `docker info --format '{{.DockerRootDir}}'` and is more robust than one,
# because the store does not have to sit under the docker data root. On the staging box
# it does not: the daemon runs the containerd snapshotter, so the upper layer is under
# /var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/ while DockerRootDir reads
# /var/lib/docker. Both are on /dev/sda1, which is also `/`.
#
# Verified against the box 2026-08-01T03:27Z: the worker container's statvfs answered
# 16,230,788 KiB available while the host's `df -Pk /` answered 16,230,784 KiB.
DISK_MEASUREMENT_PATH = "/"

_KIB = 1024


class DiskHeadroom(StrEnum):
    """How much room the box had left when it last looked."""

    OK = "ok"
    LOW = "low"
    UNKNOWN = "unknown"


def measure_free_disk_kib() -> int | None:
    """Read free space, or answer None. It does not raise, block, or shell out.

    `statvfs` rather than a `df` subprocess, and that choice removes failure modes
    instead of handling them: there is no process to fork on a box already short on
    memory, no timeout to arm, no stdout to misparse, and no locale to surprise the
    parse. What remains is a single syscall whose entire documented failure surface is
    `OSError` — a missing path, a permission denial and a filesystem that refuses all
    arrive as that one type or a subclass of it. Catching it is therefore total here,
    not a narrowing.

    Being total matters more than it looks. This runs inside `record_heartbeat`, and an
    exception escaping it would leave the liveness stamp unwritten; fifteen minutes
    later `/healthz` answers 503, the container healthcheck fails, and a *capacity
    warning* has taken the site down. That inversion is the one thing the whole disk
    signal is built to avoid, so a failed measurement degrades itself and nothing else.

    The failure is logged rather than swallowed, because "disk has read unknown for a
    week" is otherwise indistinguishable from "nobody has looked yet".
    """
    try:
        return disk_usage(DISK_MEASUREMENT_PATH).free // _KIB
    except OSError:
        logger.warning(
            "could not measure free space at %s; /healthz will report disk=unknown",
            DISK_MEASUREMENT_PATH,
            exc_info=True,
        )
        return None


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
        # Reported regardless of the measurement's age. Nothing frees space unless the
        # nightly prune runs, so an old low reading is at worst an understatement, and
        # discarding it would throw away a true alarm.
        return DiskHeadroom.LOW
    # A stale "ok" is the one answer this must never give, and the reason survived the
    # move onto the heartbeat: the writer going quiet is correlated with the disk
    # filling, because a dead scheduler is also a WAL prune that is not being triggered.
    # Reporting the last comfortable value there would be the fuse lying at the only
    # moment it is load-bearing.
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
    scheduler_stamp = scheduler[0] if scheduler else None
    # The disk reading is taken off the SCHEDULER row and nowhere else. The backup row
    # still has the column — it is one table — but nothing reads it, and ops/backup.sh
    # no longer writes it. Two writers on different cadences would disagree on any night
    # the prune actually freed space, and the probe would have no way to tell which of
    # them was describing the present.
    return HeartbeatReport(
        scheduler_alive=_is_within(scheduler_stamp, HEARTBEAT_STALE_AFTER, now),
        backup_fresh=_is_within(
            backup[0] if backup else None,
            BACKUP_STALE_AFTER,
            now,
        ),
        disk=_headroom(
            scheduler[1] if scheduler else None,
            measurement_is_fresh=_is_within(scheduler_stamp, DISK_STALE_AFTER, now),
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
    """Report how much room the box had left at the scheduler's last tick.

    Minutes old, not a night old, and that is the whole reason this rides the heartbeat.
    `ops/backup.sh` used to own the number because it runs on the host and could see the
    real filesystem — but it runs once, at 03:00. Measured against the projected floor
    crossing of 2026-08-01T21:41Z, that reading did not turn `low` until 06:07 the next
    morning: a fuse that blows eight and a half hours after the fault.

    A container can measure the right filesystem after all (see `DISK_MEASUREMENT_PATH`)
    and the reading rides the signal that was already ticking every five minutes.
    The measurement itself is taken by the scheduler, never by the probe — `/healthz` is
    hit every ten seconds by the container healthcheck and must not syscall for a number
    that only changes on beat's cadence.
    """
    return read_heartbeats().disk


def record_heartbeat() -> None:
    """Assert that the scheduler is still running, and report free space while there.

    `update_or_create` keyed on the name, so redeploys and multiple beat restarts
    move one row rather than accumulating rows nobody reads. `updated_at` is
    `auto_now`, so saving is what stamps it.

    The measurement is a bare `statvfs` that answers None on failure rather than raising
    (see `measure_free_disk_kib`). That order is deliberate and load-bearing: this is a
    liveness signal, and anything here that could fail for a reason unrelated to the
    scheduler being up would make the switch alarm on the wrong thing.
    """
    free_disk_kib = measure_free_disk_kib()
    heartbeat, created = SchedulerHeartbeat.objects.get_or_create(
        name=SCHEDULER_HEARTBEAT_NAME,
        defaults={"free_disk_kib": free_disk_kib},
    )
    if not created:
        heartbeat.free_disk_kib = free_disk_kib
        heartbeat.save(update_fields=["updated_at", "free_disk_kib"])


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
