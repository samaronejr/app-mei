"""The disk fuse: a box that is filling up must not fill up quietly.

The WAL archive grows about 4.8 GB a day on a 45 GB disk, because `archive_timeout=300`
forces a full 16 MiB segment switch every five minutes whether or not the segment has
anything in it. At `RETENTION_DAYS=7` the steady state is larger than the disk, so a
perfectly working prune still collides — and nothing anywhere reports free space, so the
first symptom would be a database that cannot write.

**This does not fix that.** Shorter retention, a longer `archive_timeout` and gzip in
`archive_command` each trade recovery window, RPO or restore procedure, and that choice
belongs to whoever owns the deployment. What this makes impossible is hitting the wall
*silently*.

**The disk reports 503 nowhere**, for the same reason the backup signal does not: a
full-ish disk needs an operator, it does not mean the application is broken. Escalating
it would flap the container healthcheck every ten seconds, fail the deploy job's fourth
assertion, and take the site down over a capacity warning.
"""

import re
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from pytest_django.fixtures import DjangoAssertNumQueries

from apps.obligations.heartbeat import (
    BACKUP_HEARTBEAT_NAME,
    MIN_FREE_DISK_KIB,
    DiskHeadroom,
    disk_headroom,
)
from apps.obligations.models import SchedulerHeartbeat

pytestmark = pytest.mark.django_db

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The deploy job states its own hard floor as a literal, so read it rather than copy it:
# if the two ever drift the fuse stops firing before deploys start failing, which is the
# one relationship that makes it useful.
DEPLOY_FLOOR = re.compile(r'^\s*MIN_FREE_KIB:\s*"(\d+)"', re.MULTILINE)

# 288 forced segment switches a day (`archive_timeout=300`) x 16 MiB each. Measured on
# the box 2026-07-31 as ~4.8 GB/day of archive growth, which is what this agrees with.
WAL_GROWTH_KIB_PER_DAY = 288 * 16 * 1024

AMPLE = MIN_FREE_DISK_KIB * 2
SCRAPING = MIN_FREE_DISK_KIB - 1

RECENT = timedelta(hours=12)
MISSED_ONE_NIGHT = timedelta(hours=37)


def record_backup(age: timedelta, free_disk_kib: int | None) -> None:
    """Stamp the marker as a run that finished `age` ago and measured `free_disk_kib`.

    `updated_at` is `auto_now`, so backdating has to go through `update` — a save would
    reset the column to now and the test would assert nothing.
    """
    SchedulerHeartbeat.objects.get_or_create(name=BACKUP_HEARTBEAT_NAME)
    SchedulerHeartbeat.objects.filter(name=BACKUP_HEARTBEAT_NAME).update(
        updated_at=timezone.now() - age,
        free_disk_kib=free_disk_kib,
    )


def test_a_fresh_marker_with_ample_headroom_reports_ok() -> None:
    # Given last night's run measuring twice the floor
    record_backup(RECENT, AMPLE)

    # When headroom is evaluated
    # Then it reports ok
    assert DiskHeadroom.OK is disk_headroom()


def test_a_fresh_marker_one_kib_under_the_floor_reports_low() -> None:
    # Given a measurement a single KiB below the floor, which is the boundary the
    # comparison is written on
    record_backup(RECENT, SCRAPING)

    # When headroom is evaluated
    # Then it reports low
    assert DiskHeadroom.LOW is disk_headroom()


def test_a_marker_written_before_this_signal_existed_reports_unknown() -> None:
    # Given a marker from a backup.sh that did not yet measure anything
    record_backup(RECENT, None)

    # When headroom is evaluated
    # Then it reports unknown rather than inventing a reassuring answer
    assert DiskHeadroom.UNKNOWN is disk_headroom()


def test_no_marker_at_all_reports_unknown() -> None:
    # Given a deployment whose nightly job has never completed
    assert not SchedulerHeartbeat.objects.filter(name=BACKUP_HEARTBEAT_NAME).exists()

    # When headroom is evaluated
    # Then it reports unknown. Unlike backup freshness — where absence IS the alarm —
    # absence here means nobody has looked, which is not the same as looking and
    # finding room.
    assert DiskHeadroom.UNKNOWN is disk_headroom()


def test_a_stale_marker_cannot_vouch_for_ample_headroom() -> None:
    # Given a comfortable measurement from a run that has not repeated since
    record_backup(MISSED_ONE_NIGHT, AMPLE)

    # When headroom is evaluated
    # Then it degrades to unknown. This is the compound failure that matters: the WAL
    # prune lives INSIDE the nightly job, so the disk fills fastest in exactly the
    # window where the marker stops being refreshed. Reporting a day-old "ok" there
    # would be the fuse lying at the only moment it is load-bearing.
    assert DiskHeadroom.UNKNOWN is disk_headroom()


def test_a_stale_marker_still_reports_low() -> None:
    # Given a low measurement from a run that has not repeated since
    record_backup(MISSED_ONE_NIGHT, SCRAPING)

    # When headroom is evaluated
    # Then it stays low rather than degrading to unknown. Free space only moves one way
    # between runs, so an old low reading is if anything an understatement — downgrading
    # it to "unknown" would discard a true alarm.
    assert DiskHeadroom.LOW is disk_headroom()


def test_the_fuse_fires_while_deploys_still_work_with_two_nights_to_act() -> None:
    # Given the deploy job's own hard floor, read out of the workflow it governs
    match = DEPLOY_FLOOR.search(WORKFLOW.read_text(encoding="utf-8"))
    assert match is not None, "deploy-staging no longer declares MIN_FREE_KIB"
    deploy_floor_kib = int(match.group(1))

    # When the fuse's floor is compared against it at the measured growth rate
    nights = (MIN_FREE_DISK_KIB - deploy_floor_kib) / WAL_GROWTH_KIB_PER_DAY

    # Then the fuse fires while the box can still accept a deploy — a warning that only
    # arrives after deploys have started failing is not a warning — and it leaves at
    # least two nightly reports to act on. Lead time is counted in REPORTS, not hours:
    # the marker is written once a night, so a floor worth less than two of them can be
    # crossed and hit the deploy floor without ever being read.
    assert deploy_floor_kib < MIN_FREE_DISK_KIB
    expected_nights = 2
    assert nights >= expected_nights


def test_the_floor_is_absolute_rather_than_a_share_of_the_disk() -> None:
    # Given the floor
    # When it is read as a quantity
    # Then it is a byte count, not a percentage. WAL accrues at a fixed 16 MiB per
    # forced switch regardless of how large the disk is, so the question an operator
    # needs answered — how many nights do I have — is free_bytes / rate, an absolute.
    # A percentage floor silently shortens the lead time on a smaller disk and
    # lengthens it on a larger one, which is backwards.
    assert MIN_FREE_DISK_KIB == 12 * 1024 * 1024


def test_healthz_reports_low_disk_without_taking_the_site_down(
    client: Client,
) -> None:
    # Given a box that measured itself under the floor last night, while the
    # application itself is perfectly healthy
    record_backup(RECENT, SCRAPING)

    # When the probe is called
    response = client.get(reverse("healthz"))

    # Then the condition is visible in the body and the probe still answers 200 with
    # status "ok". The container healthcheck and the deploy job's fourth assertion both
    # key off exactly those two things, so anything else would convert an operator task
    # into an outage and a blocked deploy.
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "status": "ok",
        "scheduler": "alive",
        "backup": "fresh",
        "disk": "low",
    }


def test_healthz_reports_ample_disk_as_ok(client: Client) -> None:
    # Given last night's run measuring room to spare
    record_backup(RECENT, AMPLE)

    # When the probe is called
    response = client.get(reverse("healthz"))

    # Then the field reads ok, so the low reading above is a real transition an operator
    # can see happen rather than a value the endpoint always prints
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "status": "ok",
        "scheduler": "alive",
        "backup": "fresh",
        "disk": "ok",
    }


def test_a_low_disk_does_not_soften_the_scheduler_switch(client: Client) -> None:
    # Given a dead scheduler on a box that is also running out of room
    SchedulerHeartbeat.objects.filter(name="beat").update(
        updated_at=timezone.now() - timedelta(minutes=16),
    )
    record_backup(RECENT, SCRAPING)

    # When the probe is called
    response = client.get(reverse("healthz"))

    # Then the scheduler still answers 503. Adding a third signal must not dilute the
    # only one that is allowed to fail the probe.
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {
        "status": "degraded",
        "scheduler": "stale",
        "backup": "fresh",
        "disk": "low",
    }


def test_reading_all_three_signals_still_costs_one_query(
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    # Given a marker carrying a disk measurement
    record_backup(RECENT, AMPLE)

    # When the report is read
    # Then it costs exactly one query. /healthz is hit by the container healthcheck
    # every ten seconds against two gunicorn workers and twenty-four connection slots,
    # so the budget is a real constraint. Exactly one, never "at most one": a budget
    # written as `<=` also passes at ZERO reads, which is what a probe that stopped
    # touching the table at all would produce.
    with django_assert_num_queries(1):
        disk_headroom()
