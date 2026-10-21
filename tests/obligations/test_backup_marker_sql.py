"""The backup freshness marker must stay writable as the heartbeat table grows.

`ops/backup.sh` writes its marker with raw SQL, because it runs on the host with no
Python environment. Django never sees that statement: when obligations 0022 added NOT
NULL outcome columns without database defaults, every model write kept working and the
script's insert started failing. The pilot then made verified local backups for five
nights while /healthz reported them stale, and the off-host copy, which runs after the
marker, never ran.

So these tests execute the script's own statement, read out of the file, against the
migrated schema.
"""

import re
from datetime import timedelta
from pathlib import Path

import pytest
from django.db import connection
from django.utils import timezone

from apps.obligations.heartbeat import BACKUP_HEARTBEAT_NAME, backup_is_fresh
from apps.obligations.models import SchedulerHeartbeat

pytestmark = pytest.mark.django_db

BACKUP_SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "backup.sh"
MARKER_SQL = re.compile(
    r'-qtAc "(?P<sql>[^"]*insert into obligations_schedulerheartbeat[^"]*)"',
)


def marker_sql() -> str:
    """Return the marker statement exactly as backup.sh hands it to psql."""
    match = MARKER_SQL.search(BACKUP_SCRIPT.read_text(encoding="utf-8"))
    assert match is not None, "backup.sh no longer writes the marker with psql -qtAc"
    sql = match.group("sql")
    # Inside double quotes the shell would expand these, so running the text as read
    # would test a different statement from the one psql actually receives.
    assert "$" not in sql
    assert "`" not in sql
    return sql


def record_marker() -> None:
    """Run the marker statement the way the nightly backup does."""
    with connection.cursor() as cursor:
        cursor.execute(marker_sql())


def test_the_marker_creates_the_backup_row_on_a_new_deployment() -> None:
    # Given a deployment that has never recorded a backup
    assert not SchedulerHeartbeat.objects.filter(name=BACKUP_HEARTBEAT_NAME).exists()

    # When backup.sh records its marker
    record_marker()

    # Then the backup reads fresh
    assert backup_is_fresh() is True


def test_the_marker_refreshes_an_existing_stale_row() -> None:
    # Given the pilot's state: a backup row last stamped two nights ago
    SchedulerHeartbeat.objects.create(name=BACKUP_HEARTBEAT_NAME)
    SchedulerHeartbeat.objects.filter(name=BACKUP_HEARTBEAT_NAME).update(
        updated_at=timezone.now() - timedelta(hours=48),
    )
    assert backup_is_fresh() is False

    # When backup.sh records its marker
    record_marker()

    # Then the existing row is refreshed and the backup reads fresh
    assert backup_is_fresh() is True
