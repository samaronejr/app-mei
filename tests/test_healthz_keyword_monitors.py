"""The external monitor's keywords are exact rendered bytes, and this is what pins them.

UptimeRobot cannot evaluate JSONPath. Its only body-inspection primitive is a plain
substring match, so the four `/healthz` signals cannot be watched by one monitor with a
JSON expression; each field gets its own keyword monitor, alerting on the ABSENCE of its
healthy value. `ops/README.md`, "External uptime monitoring", is the configuration those
monitors are built from, and this module is what stops it from being fiction.

**The failure this module exists to make loud is a missing space.** Django's
`JsonResponse` serializes with `json.dumps` default separators, so the body reads
`{"status": "ok", ...}` with a space after every colon. A keyword typed the way it is
written in code, `"backup":"fresh"`, matches nothing at all: configured to alert on
absence it sits permanently DOWN, and configured the other way round it sits permanently
UP while asserting nothing. Neither state is distinguishable from a working monitor
without looking at the bytes, which is what the assertions below do.

So the keywords are read OUT OF `ops/README.md` rather than copied into Python, and
compared against a body produced by a real request. A space deleted from the operator's
copy-paste block turns this suite red; so does an endpoint that stops rendering any of
them; so does a serializer change that drops the separators.

**The complete serialized document is deliberately NOT pinned here.** Four fragments and
four semantic values are asserted individually, because a monitor watches one fragment
at a time and an equality on the whole rendered string would fail for reasons that have
nothing to do with whether any monitor still works. `tests/test_health.py` and the two
heartbeat modules already own the whole-body contract.
"""

import re
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Final

import pytest
from django.db import OperationalError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.core import views
from apps.obligations.heartbeat import (
    BACKUP_HEARTBEAT_NAME,
    HEARTBEAT_STALE_AFTER,
    MIN_FREE_DISK_KIB,
    SCHEDULER_HEARTBEAT_NAME,
)
from apps.obligations.models import SchedulerHeartbeat

pytestmark = pytest.mark.django_db

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
OPS_README: Final[Path] = REPO_ROOT / "ops" / "README.md"
VERIFY_LIVE: Final[Path] = REPO_ROOT / ".github" / "workflows" / "verify-live.yml"

# The operator's copy-paste blocks are anchored by HTML comments rather than by heading
# text, so re-wording the prose around them cannot silently detach this parse from the
# values it is meant to read.
KEYWORD_MARKER: Final = "<!-- healthz-keyword-monitors -->"
NEGATIVE_CONTROL_MARKER: Final = "<!-- healthz-keyword-negative-control -->"
FENCED_BLOCK: Final = re.compile(r"```text\n(.*?)\n```", re.DOTALL)

# What the four monitors are configured with, written out once so that a diff of this
# file shows the monitor configuration changing. The assertion that these ARE the
# documented values is a test below, not an import.
MONITORED_FRAGMENTS: Final = (
    '"status": "ok"',
    '"scheduler": "alive"',
    '"backup": "fresh"',
    '"disk": "ok"',
)

# The fifth monitor. Same URL, same alert-on-absence setting, a value `scheduler` can
# never take -- so it must sit permanently DOWN, which is what proves the other four are
# green because a keyword matched rather than because nothing is being evaluated.
IMPOSSIBLE_FRAGMENT: Final = '"scheduler": "impossible-keyword-negative-control"'

FRESH = timedelta(minutes=5)
STALLED = HEARTBEAT_STALE_AFTER + timedelta(minutes=1)
BACKUP_RECENT = timedelta(hours=12)
BACKUP_MISSED_ONE_NIGHT = timedelta(hours=37)
AMPLE = MIN_FREE_DISK_KIB * 2
SCRAPING = MIN_FREE_DISK_KIB - 1


def stamp_beat(age: timedelta, free_disk_kib: int | None) -> None:
    """Write the scheduler row as a tick `age` ago that measured `free_disk_kib`.

    `updated_at` is `auto_now`, so the backdating goes through `update`; a save would
    reset the column to now and the test would assert nothing.
    """
    SchedulerHeartbeat.objects.get_or_create(name=SCHEDULER_HEARTBEAT_NAME)
    SchedulerHeartbeat.objects.filter(name=SCHEDULER_HEARTBEAT_NAME).update(
        updated_at=timezone.now() - age,
        free_disk_kib=free_disk_kib,
    )


def stamp_backup(age: timedelta) -> None:
    """Write the nightly marker as a run that finished `age` ago."""
    SchedulerHeartbeat.objects.get_or_create(name=BACKUP_HEARTBEAT_NAME)
    SchedulerHeartbeat.objects.filter(name=BACKUP_HEARTBEAT_NAME).update(
        updated_at=timezone.now() - age,
    )


def make_healthy() -> None:
    """Put all four signals into the state the four monitors expect to find.

    The suite's default database is deliberately NOT this state -- it has never run a
    backup and has never measured a disk -- so the healthy body has to be constructed
    rather than assumed. That is the same reason `tests/test_health.py` records
    `backup: stale` and `disk: unknown` as its baseline.
    """
    stamp_beat(FRESH, AMPLE)
    stamp_backup(BACKUP_RECENT)


def rendered_body(client: Client) -> str:
    """Return the bytes `/healthz` actually put on the wire, decoded and nothing else.

    Never `json.dumps(response.json())`. Re-serializing would launder exactly the
    separator detail these assertions exist to measure, and every fragment below would
    then match a body the monitor would have missed.
    """
    return client.get(reverse("healthz")).content.decode()


def documented_block(marker: str) -> tuple[str, ...]:
    """Read the operator's copy-paste block that follows `marker` in `ops/README.md`."""
    text = OPS_README.read_text(encoding="utf-8")
    assert marker in text, f"ops/README.md no longer carries {marker}"
    fence = FENCED_BLOCK.search(text[text.index(marker) :])
    assert fence is not None, f"no fenced text block follows {marker}"
    return tuple(line for line in fence.group(1).splitlines() if line.strip())


def unspaced(fragment: str) -> str:
    """Return the fragment as somebody would type it from memory, without the space."""
    return fragment.replace('": "', '":"')


def keyword_key(fragment: str) -> str:
    """Return the `/healthz` field a keyword fragment is watching."""
    return fragment.split('": "', maxsplit=1)[0].lstrip('"')


def test_the_documented_keywords_are_the_ones_this_module_pins() -> None:
    # Given the block an operator copies into UptimeRobot
    documented = documented_block(KEYWORD_MARKER)

    # When it is compared with the four fragments asserted below
    # Then they are the same strings in the same order. This is the join that makes
    # every other assertion in this file an assertion about the real monitors: without
    # it the tests would pin four literals nobody configures, and the documented block
    # could drift a space at a time with nothing watching.
    assert documented == MONITORED_FRAGMENTS


def test_the_documented_negative_control_is_the_one_this_module_pins() -> None:
    # Given the impossible-keyword block
    documented = documented_block(NEGATIVE_CONTROL_MARKER)

    # When it is compared with the pinned control
    # Then they agree, and the control is exactly one keyword. A second line here would
    # be a second monitor nobody has an expected state for.
    assert documented == (IMPOSSIBLE_FRAGMENT,)


def test_every_monitored_keyword_is_a_literal_substring_of_a_healthy_body(
    client: Client,
) -> None:
    # Given a deployment with all four signals healthy
    make_healthy()

    # When the body is read as bytes rather than as parsed JSON
    body = rendered_body(client)

    # Then each keyword is found in it exactly as the monitor would search for it. This
    # is the whole contract: UptimeRobot does a substring match and nothing else, so a
    # fragment that is not literally present is a monitor that never matches.
    for fragment in MONITORED_FRAGMENTS:
        assert fragment in body, f"{fragment!r} is not in {body!r}"


def test_a_keyword_without_the_space_after_the_colon_matches_nothing(
    client: Client,
) -> None:
    # Given the same healthy body
    make_healthy()
    body = rendered_body(client)

    for fragment in MONITORED_FRAGMENTS:
        # When the space after the colon is removed, which is how the fragment gets
        # written when it is retyped from code rather than copied from the response
        mistyped = unspaced(fragment)

        # Then the mutation is real, and it matches nothing. Configured to alert on
        # absence this reads permanently DOWN; configured to alert on presence it reads
        # permanently UP while asserting nothing at all, and that second failure is
        # invisible from the monitor list. This assertion is why the fragments above are
        # not merely "some strings that happen to be in the body".
        assert mistyped != fragment, f"{fragment!r} carries no space to remove"
        assert mistyped not in body, f"{mistyped!r} unexpectedly matched {body!r}"


def test_the_healthy_body_reports_each_field_semantically(client: Client) -> None:
    # Given a deployment with all four signals healthy
    make_healthy()

    # When the probe is called
    response = client.get(reverse("healthz"))
    payload = response.json()

    # Then each field carries its healthy value, asserted one field at a time. The
    # keyword monitors watch the rendered bytes; these are the same four claims read
    # semantically, so a serializer change that altered the bytes without altering the
    # meaning would show up as exactly one of the two halves failing.
    assert response.status_code == HTTPStatus.OK
    assert payload["status"] == "ok"
    assert payload["scheduler"] == "alive"
    assert payload["backup"] == "fresh"
    assert payload["disk"] == "ok"


def test_the_four_monitors_cover_every_field_the_body_renders(client: Client) -> None:
    # Given the healthy body
    make_healthy()
    payload = client.get(reverse("healthz")).json()

    # When each keyword's key is taken from the fragment itself
    monitored_keys = {keyword_key(fragment) for fragment in MONITORED_FRAGMENTS}

    # Then the four monitors cover the four fields, one each. A fifth field added to
    # /healthz would be a signal with no external monitor watching it, and this is what
    # forces that decision to be made rather than skipped.
    assert len(monitored_keys) == len(MONITORED_FRAGMENTS)
    assert monitored_keys == set(payload)


def test_each_keyword_disappears_when_the_signal_behind_it_degrades(
    client: Client,
) -> None:
    # Given a healthy deployment as the control
    make_healthy()
    assert '"backup": "fresh"' in rendered_body(client)
    assert '"disk": "ok"' in rendered_body(client)

    # When the nightly backup misses a night
    stamp_backup(BACKUP_MISSED_ONE_NIGHT)

    # Then M1c's keyword is gone and the monitor goes DOWN, while the probe stays 200 --
    # which is exactly why this is watched with a keyword monitor and not an HTTP one.
    response = client.get(reverse("healthz"))
    assert response.status_code == HTTPStatus.OK
    assert '"backup": "fresh"' not in response.content.decode()

    # And when the box measures itself under the floor instead
    make_healthy()
    stamp_beat(FRESH, SCRAPING)

    # Then M1d's keyword is gone, also at 200
    response = client.get(reverse("healthz"))
    assert response.status_code == HTTPStatus.OK
    assert '"disk": "ok"' not in response.content.decode()


def test_a_stopped_scheduler_takes_three_of_the_four_keywords_with_it(
    client: Client,
) -> None:
    # Given a deployment whose beat container has stopped, on a box that had ample room
    # when it last looked, with the nightly backup still running from its own timer
    make_healthy()
    stamp_beat(STALLED, AMPLE)

    # When the probe is called
    response = client.get(reverse("healthz"))
    body = response.content.decode()

    # Then M1a, M1b AND M1d all go DOWN together, and only M1c stays UP. The third of
    # those is the one nobody expects: a stalled writer cannot vouch for the disk
    # reading it last wrote, so `disk` degrades from `ok` to `unknown` on the
    # scheduler's own window -- see `_headroom` and `DISK_STALE_AFTER`. An operator
    # watching the beat drill sees three monitors turn red from one cause, and reading
    # that as three faults is how a capacity page gets chased during a scheduler outage.
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert '"status": "ok"' not in body
    assert '"scheduler": "alive"' not in body
    assert '"disk": "ok"' not in body
    assert '"backup": "fresh"' in body


def test_the_impossible_keyword_can_never_be_rendered_by_any_state(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the negative control keyword, which differs from a real fragment only in its
    # VALUE -- its key is a field the endpoint really renders, so a monitor that matched
    # on the key alone would report UP and be caught here rather than in production
    key = IMPOSSIBLE_FRAGMENT.split('": "')[0].lstrip('"')
    make_healthy()
    assert key in client.get(reverse("healthz")).json()
    assert IMPOSSIBLE_FRAGMENT not in MONITORED_FRAGMENTS

    # When every state /healthz can report is rendered in turn
    make_healthy()
    healthy = rendered_body(client)

    stamp_beat(STALLED, AMPLE)
    degraded = rendered_body(client)

    refused = "connection refused"

    def explode() -> object:
        raise OperationalError(refused)

    # Patched where it is USED: apps.core.views binds the name at import, so patching
    # the source module would leave the view calling the original.
    monkeypatch.setattr(views, "read_heartbeats", explode)
    unreadable = rendered_body(client)

    # Then the keyword appears in none of them, so the monitor is DOWN in every state
    # the application can reach. That permanent DOWN is the assertion: it is the only
    # thing in the monitor list that demonstrates keyword matching is happening at all,
    # and four UP rows beside it mean nothing without it.
    for state, body in (
        ("healthy", healthy),
        ("degraded", degraded),
        ("heartbeat table unreadable", unreadable),
    ):
        assert IMPOSSIBLE_FRAGMENT not in body, f"{state} body was {body!r}"


def test_the_scheduled_workflow_checks_the_same_fragments_the_monitors_do() -> None:
    # Given the scheduled live-verification workflow
    workflow = VERIFY_LIVE.read_text(encoding="utf-8")

    # When it is read for the keyword fragments
    # Then it carries all four, plus the unspaced form of one as its own negative
    # control. UptimeRobot's configuration is external state this repository cannot see,
    # so the workflow checking the identical strings daily is what makes a drift between
    # the endpoint and the monitors visible from inside CI rather than only from a
    # monitor that quietly stopped matching.
    for fragment in MONITORED_FRAGMENTS:
        assert fragment in workflow, f"verify-live.yml no longer checks {fragment!r}"
    assert unspaced('"backup": "fresh"') in workflow
