"""Serialize SITFIS wire admission and durable replies without a transaction."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

from django.db import connection
from psycopg import OperationalError, connect

from apps.core.tasks import assert_no_transaction

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

WIRE_LOCK_WAIT_MS: Final = 70_000


class ReportWireBusyError(Exception):
    """The report's bounded session-lock acquisition failed before any send."""


@contextmanager
def report_mutex(
    tenant_id: UUID, report_id: UUID, *, timeout_ms: int = WIRE_LOCK_WAIT_MS
) -> Iterator[None]:
    """Hold one report's session mutex; closing its connection always releases it.

    This dedicated connection is never used for tenant data or transactions.
    Writers acquire it before entering their tenant transaction. The wire guard
    commits its short eligibility transaction before yielding to the transport.
    A timeout, exception or process death cannot leave a durable permit behind.
    """
    assert_no_transaction()
    parameters = connection.get_connection_params()
    parameters["autocommit"] = True
    parameters["connect_timeout"] = 5
    try:
        mutex = connect(**parameters)
    except OperationalError:
        raise ReportWireBusyError from None
    try:
        try:
            with mutex.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
                cursor.execute(
                    "SELECT set_config('lock_timeout', %s, false)",
                    [f"{timeout_ms}ms"],
                )
                cursor.execute(
                    "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                    [f"sitfis-wire:{tenant_id}:{report_id}"],
                )
        except OperationalError:
            raise ReportWireBusyError from None
        yield
    finally:
        mutex.close()
