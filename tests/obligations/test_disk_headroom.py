"""The disk fuse: a box that is filling up must not fill up quietly, or LATE.

The WAL archive grows about 4.8 GB a day on a 45 GB disk, because `archive_timeout=300`
forces a full 16 MiB segment switch every five minutes whether or not the segment has
anything in it. At `RETENTION_DAYS=7` the steady state is larger than the disk, so a
perfectly working prune still collides — and nothing anywhere reported free space, so
the first symptom would have been a database that cannot write.

**This does not fix that.** Shorter retention, a longer `archive_timeout` and gzip in
`archive_command` each trade recovery window, RPO or restore procedure, and that choice
belongs to whoever owns the deployment. What this makes impossible is hitting the wall
*silently* — or *late*.

**Late is the second bug, and it is the one this file was rewritten for.** The reading
used to be stamped by `ops/backup.sh`, once a night at 03:00 local. Measured against the
projected floor crossing of 2026-08-01T21:41Z, the fuse would not have read `low` until
2026-08-02T06:07Z — a warning that arrives 8.5 hours after the event it warns about. The
measurement now rides the scheduler heartbeat, which already ticks every five minutes,
so the same crossing is visible within one tick plus its decay window.

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

from apps.obligations import heartbeat as heartbeat_module
from apps.obligations.heartbeat import (
    BACKUP_HEARTBEAT_NAME,
    BACKUP_STALE_AFTER,
    DISK_STALE_AFTER,
    HEARTBEAT_INTERVAL,
    HEARTBEAT_STALE_AFTER,
    MIN_FREE_DISK_KIB,
    SCHEDULER_HEARTBEAT_NAME,
    DiskHeadroom,
    disk_headroom,
    measure_free_disk_kib,
    record_heartbeat,
    scheduler_is_alive,
)
from apps.obligations.models import SchedulerHeartbeat

pytestmark = pytest.mark.django_db

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
BACKUP_SCRIPT = REPO_ROOT / "ops" / "backup.sh"

# The deploy job states its own hard floor as a literal, so read it rather than copy it:
# if the two ever drift the fuse stops firing before deploys start failing, which is the
# one relationship that makes it useful.
DEPLOY_FLOOR = re.compile(r'^\s*MIN_FREE_KIB:\s*"(\d+)"', re.MULTILINE)

# 288 forced segment switches a day (`archive_timeout=300`) x 16 MiB each. Measured on
# the box 2026-07-31 as ~4.8 GB/day of archive growth, which is what this agrees with.
WAL_GROWTH_KIB_PER_DAY = 288 * 16 * 1024

AMPLE = MIN_FREE_DISK_KIB * 2
SCRAPING = MIN_FREE_DISK_KIB - 1

# One tick. The scheduler reports in every five minutes, so this is what "the reading
# the probe is looking at right now" actually looks like in production.
RECENT = HEARTBEAT_INTERVAL
# Three missed ticks. Past this the reading is no longer something a running scheduler
# is standing behind.
STALLED = HEARTBEAT_STALE_AFTER + timedelta(minutes=1)


def record_beat(age: timedelta, free_disk_kib: int | None) -> None:
    """Stamp the beat row as a tick that happened `age` ago and saw `free_disk_kib`.

    `updated_at` is `auto_now`, so backdating has to go through `update` — a save would
    reset the column to now and the test would assert nothing.
    """
    SchedulerHeartbeat.objects.get_or_create(name=SCHEDULER_HEARTBEAT_NAME)
    SchedulerHeartbeat.objects.filter(name=SCHEDULER_HEARTBEAT_NAME).update(
        updated_at=timezone.now() - age,
        free_disk_kib=free_disk_kib,
    )


def record_backup(age: timedelta, free_disk_kib: int | None) -> None:
    """Stamp the nightly marker, including a disk column no longer anybody reads."""
    SchedulerHeartbeat.objects.get_or_create(name=BACKUP_HEARTBEAT_NAME)
    SchedulerHeartbeat.objects.filter(name=BACKUP_HEARTBEAT_NAME).update(
        updated_at=timezone.now() - age,
        free_disk_kib=free_disk_kib,
    )


def test_a_fresh_reading_with_ample_headroom_reports_ok() -> None:
    # Given the last tick measuring twice the floor
    record_beat(RECENT, AMPLE)

    # When headroom is evaluated
    # Then it reports ok
    assert DiskHeadroom.OK is disk_headroom()


def test_a_fresh_reading_one_kib_under_the_floor_reports_low() -> None:
    # Given a measurement a single KiB below the floor, which is the boundary the
    # comparison is written on
    record_beat(RECENT, SCRAPING)

    # When headroom is evaluated
    # Then it reports low
    assert DiskHeadroom.LOW is disk_headroom()


def test_a_row_written_before_this_signal_existed_reports_unknown() -> None:
    # Given a heartbeat from a build that did not yet measure anything
    record_beat(RECENT, None)

    # When headroom is evaluated
    # Then it reports unknown rather than inventing a reassuring answer
    assert DiskHeadroom.UNKNOWN is disk_headroom()


def test_no_heartbeat_at_all_reports_unknown() -> None:
    # Given a deployment whose scheduler has never ticked
    SchedulerHeartbeat.objects.all().delete()

    # When headroom is evaluated
    # Then it reports unknown. Unlike scheduler liveness — where absence IS the alarm —
    # absence here means nobody has looked, which is not the same as looking and
    # finding room.
    assert DiskHeadroom.UNKNOWN is disk_headroom()


def test_a_stalled_heartbeat_cannot_vouch_for_ample_headroom() -> None:
    # Given a comfortable measurement from a scheduler that has since gone quiet
    record_beat(STALLED, AMPLE)

    # When headroom is evaluated
    # Then it degrades to unknown. This is the compound failure that matters and it
    # survived the move off the nightly marker unchanged: a dead writer means nobody is
    # watching the number, and a dead scheduler ALSO means the WAL prune's own trigger
    # is not running — so the disk fills fastest in exactly the window where the reading
    # stops being refreshed. Reporting the last comfortable value there would be the
    # fuse lying at the only moment it is load-bearing.
    #
    # Deleting the freshness argument from `_headroom` turns this red, which is the
    # regression this test exists to catch.
    assert DiskHeadroom.UNKNOWN is disk_headroom()


def test_a_stalled_heartbeat_still_reports_low() -> None:
    # Given a low measurement from a scheduler that has since gone quiet
    record_beat(STALLED, SCRAPING)

    # When headroom is evaluated
    # Then it stays low rather than degrading to unknown. Free space only moves one way
    # while nothing is pruning, so an old low reading is if anything an understatement —
    # downgrading it to "unknown" would discard a true alarm.
    assert DiskHeadroom.LOW is disk_headroom()


def test_the_reading_ages_out_in_minutes_rather_than_overnight() -> None:
    # Given the window the disk reading is trusted within
    # When it is compared with the two cadences in this module
    # Then it is the SCHEDULER's window, not the nightly job's. This is the whole fix:
    # against the projected 2026-08-01T21:41Z floor crossing, a 36-hour window anchored
    # to a 03:00 job first read `low` at 06:07 the following morning — 8.5 hours late.
    assert DISK_STALE_AFTER == HEARTBEAT_STALE_AFTER
    assert DISK_STALE_AFTER < BACKUP_STALE_AFTER

    # And the worst-case lag from a crossing to a red field is one missed tick plus the
    # window, which is well under an hour rather than most of a night.
    worst_case_lag = HEARTBEAT_INTERVAL + DISK_STALE_AFTER
    assert worst_case_lag <= timedelta(hours=1)


def test_the_heartbeat_records_free_space_on_every_tick() -> None:
    # Given a scheduler row carrying no measurement
    record_beat(RECENT, None)

    # When beat reports in
    record_heartbeat()

    # Then the tick carried a real reading with it. The measurement rides the signal
    # that already runs continuously, which is what makes the field minutes-fresh;
    # dropping it back out of `record_heartbeat` turns this red.
    measured = SchedulerHeartbeat.objects.get(
        name=SCHEDULER_HEARTBEAT_NAME,
    ).free_disk_kib
    assert measured is not None
    assert measured > 0


def test_a_reading_on_the_backup_row_cannot_answer_for_the_disk() -> None:
    # Given a nightly marker still carrying a comfortable number from an older build,
    # and a scheduler that has measured nothing
    record_backup(timedelta(hours=1), AMPLE)
    record_beat(RECENT, None)

    # When headroom is evaluated
    # Then it is unknown, because exactly one row owns this column now. Two writers
    # disagreeing silently is the failure this forecloses: if `read_heartbeats` were
    # ever re-pointed at the backup row, or taught to fall back to it, this turns red.
    assert DiskHeadroom.UNKNOWN is disk_headroom()


def test_a_measurement_that_cannot_be_taken_reports_unknown_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a filesystem that refuses to answer — a missing path, a permission error
    # and a failing statvfs all arrive here as OSError, which is the complete failure
    # surface of the syscall this is built on
    def refuse(_path: str) -> object:
        msg = "no such file or directory"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(heartbeat_module, "disk_usage", refuse)

    # When the measurement is taken
    # Then it answers "I do not know" instead of propagating
    assert measure_free_disk_kib() is None


def test_a_failed_measurement_still_lets_the_scheduler_report_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a scheduler that has gone quiet and a filesystem that cannot be read
    record_beat(STALLED, AMPLE)

    def refuse(_path: str) -> object:
        msg = "permission denied"
        raise PermissionError(msg)

    monkeypatch.setattr(heartbeat_module, "disk_usage", refuse)

    # When beat reports in
    record_heartbeat()

    # Then the liveness signal still lands. This is the inversion the design forbids: a
    # capacity measurement that could abort the heartbeat would age the scheduler out,
    # answer 503, fail the container healthcheck and take the site down — turning a
    # warning about the disk into an outage.
    assert scheduler_is_alive() is True
    assert DiskHeadroom.UNKNOWN is disk_headroom()


def test_the_probe_never_touches_the_filesystem(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a measurement that would fail loudly if the request path ever took it
    record_beat(RECENT, AMPLE)
    detonated = "the request path measured the filesystem"

    def detonate(_path: str) -> object:
        raise AssertionError(detonated)

    monkeypatch.setattr(heartbeat_module, "disk_usage", detonate)

    # When the probe is called
    response = client.get(reverse("healthz"))

    # Then it answers from the stored reading and never syscalls. /healthz is hit by the
    # container healthcheck every ten seconds against two gunicorn workers and four
    # concurrent request slots; measuring there would put a blocking syscall on the
    # liveness path for a number that changes on a five-minute cadence anyway.
    assert response.status_code == HTTPStatus.OK
    assert response.json()["disk"] == "ok"


def test_the_fuse_fires_while_deploys_still_work_with_two_nights_to_act() -> None:
    # Given the deploy job's own hard floor, read out of the workflow it governs
    match = DEPLOY_FLOOR.search(WORKFLOW.read_text(encoding="utf-8"))
    assert match is not None, "deploy-staging no longer declares MIN_FREE_KIB"
    deploy_floor_kib = int(match.group(1))

    # When the fuse's floor is compared against it at the measured growth rate
    nights = (MIN_FREE_DISK_KIB - deploy_floor_kib) / WAL_GROWTH_KIB_PER_DAY

    # Then the fuse fires while the box can still accept a deploy — a warning that only
    # arrives after deploys have started failing is not a warning — and it leaves at
    # least two nights to act on. Making the reading minutes-fresh did NOT shrink the
    # floor: what it bought is that the crossing is seen when it happens, not that it
    # needs less runway. The action the fuse demands is a capacity decision with real
    # trade-offs (retention window, RPO, restore procedure), which is measured in days
    # of an operator's attention, not in minutes of alert latency.
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


def test_the_floor_is_derived_from_the_deploy_floor_and_the_archive_rate() -> None:
    # Given the deploy job's floor, read out of the workflow rather than copied
    match = DEPLOY_FLOOR.search(WORKFLOW.read_text(encoding="utf-8"))
    assert match is not None, "deploy-staging no longer declares MIN_FREE_KIB"
    deploy_floor_kib = int(match.group(1))

    # When the derivation is recomputed from its two inputs
    nights_of_runway = 2
    derived = deploy_floor_kib + nights_of_runway * WAL_GROWTH_KIB_PER_DAY

    # Then it IS the floor, arithmetically. 3 GiB + 2 x 4.5 GiB = 12 GiB. There is
    # exactly one copy of this number in Python and it is the one being checked here,
    # so a second hardcoded floor added anywhere would have to disagree with this to
    # exist at all.
    assert derived == MIN_FREE_DISK_KIB


def test_the_nightly_job_no_longer_stamps_a_second_disk_reading() -> None:
    # Given the executable body of the host-side backup script, with its commentary
    # stripped — the comment block there explains the removal and names the column, so
    # a whole-file search would be answered by the very note that documents the fix
    body = "\n".join(
        line
        for line in BACKUP_SCRIPT.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )

    # When it is read for writers of the disk column
    # Then there are none. The scheduler owns `free_disk_kib` outright; two writers on
    # different cadences would disagree with each other on any night the backup ran
    # after a prune, and nothing in the probe could tell which one was right.
    assert "free_disk_kib" not in body
    assert "df -Pk" not in body

    # And the marker it does still write is unaffected — the backup freshness signal is
    # a separate switch and keeps its own row.
    assert "obligations_schedulerheartbeat" in body


def test_healthz_reports_low_disk_without_taking_the_site_down(
    client: Client,
) -> None:
    # Given a box that measured itself under the floor minutes ago, while the
    # application itself is perfectly healthy
    record_beat(RECENT, SCRAPING)

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
        "backup": "stale",
        "disk": "low",
    }


def test_healthz_reports_ample_disk_as_ok(client: Client) -> None:
    # Given the last tick measuring room to spare
    record_beat(RECENT, AMPLE)

    # When the probe is called
    response = client.get(reverse("healthz"))

    # Then the field reads ok, so the low reading above is a real transition an operator
    # can see happen rather than a value the endpoint always prints
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "status": "ok",
        "scheduler": "alive",
        "backup": "stale",
        "disk": "ok",
    }


def test_healthz_stays_200_when_the_disk_cannot_be_measured(client: Client) -> None:
    # Given a healthy scheduler whose measurement did not come back
    record_beat(RECENT, None)

    # When the probe is called
    response = client.get(reverse("healthz"))

    # Then the probe is 200 and says so. A measurement this endpoint cannot take is not
    # a reason to restart the web process.
    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == "ok"
    assert response.json()["disk"] == "unknown"


def test_a_low_disk_does_not_soften_the_scheduler_switch(client: Client) -> None:
    # Given a dead scheduler on a box that was also running out of room when it died
    record_beat(STALLED, SCRAPING)

    # When the probe is called
    response = client.get(reverse("healthz"))

    # Then the scheduler still answers 503, and the low reading survives the writer's
    # death rather than being softened to unknown. A third signal must not dilute the
    # only one that is allowed to fail the probe, in either direction.
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {
        "status": "degraded",
        "scheduler": "stale",
        "backup": "stale",
        "disk": "low",
    }


def test_a_low_disk_never_answers_503_while_the_scheduler_is_alive(
    client: Client,
) -> None:
    # Given every disk state this fuse can report, against a live scheduler
    for free_kib in (SCRAPING, AMPLE, None, 0):
        record_beat(RECENT, free_kib)

        # When the probe is called
        response = client.get(reverse("healthz"))

        # Then it is 200 every time. `disk` is wired to the body and to nothing else;
        # promoting it into the status expression would be caught here regardless of
        # which value the promotion keyed on.
        assert response.status_code == HTTPStatus.OK, f"free_kib={free_kib}"
        assert response.json()["status"] == "ok", f"free_kib={free_kib}"


def test_reading_all_three_signals_still_costs_one_query(
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    # Given a heartbeat carrying a disk measurement
    record_beat(RECENT, AMPLE)

    # When the report is read
    # Then it costs exactly one query. /healthz is hit by the container healthcheck
    # every ten seconds against two gunicorn workers and twenty-four connection slots,
    # so the budget is a real constraint. Exactly one, never "at most one": a budget
    # written as `<=` also passes at ZERO reads, which is what a probe that stopped
    # touching the table at all would produce.
    with django_assert_num_queries(1):
        disk_headroom()
