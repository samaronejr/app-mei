"""The final guard holds no transaction and cannot strand report ownership."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from django.db import connection
from psycopg import connect
from psycopg.pq import TransactionStatus

from apps.integrations import transport, transport_wire
from apps.integrations.serpro import sitfis_wire
from apps.integrations.serpro.sitfis_wire import ReportWireBusyError, report_mutex
from tests.integrations.sitfis_support import (
    World,
    clock,
    protocol_reply,
    report_reply,
    world,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from psycopg import Connection
    from requests import Request

    from apps.integrations.transport_types import ProviderResponse
    from apps.integrations.transport_wire import WireLimits

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "world"]


def _observe[**P, T](
    callback: Callable[P, T], inspect: Callable[[T], None]
) -> Callable[P, T]:
    def observed(*args: P.args, **kwargs: P.kwargs) -> T:
        result = callback(*args, **kwargs)
        inspect(result)
        return result

    return observed


@pytest.mark.parametrize("timeout", [False, True])
def test_wire_is_transaction_free_and_timeout_releases_report_ownership(
    world: World, monkeypatch: pytest.MonkeyPatch, *, timeout: bool
) -> None:
    world.script(protocol_reply(30_000))
    report = world.advance(world.request())
    world.clock.advance(seconds=30)
    world.script(report_reply())
    sessions: list[Connection[tuple[object, ...]]] = []
    monkeypatch.setattr(sitfis_wire, "connect", _observe(connect, sessions.append))
    inspected: list[int] = []

    def exchange(
        request: Request, limits: WireLimits, *, redirect: bool = False
    ) -> ProviderResponse | str:
        assert not connection.in_atomic_block
        assert connection.get_autocommit()
        assert connection.connection is not None
        assert connection.connection.info.transaction_status == TransactionStatus.IDLE
        [active] = [session for session in sessions if not session.closed]
        assert active.autocommit
        assert active.info.transaction_status == TransactionStatus.IDLE
        inspected.append(active.info.backend_pid)
        reply = transport_wire._exchange(request, limits, redirect=redirect)
        if timeout:
            raise TimeoutError
        return reply

    monkeypatch.setattr(transport, "_exchange", exchange)
    world.advance(report)
    assert len(inspected) == 1
    assert len(world.sent("/Emitir")) == 1
    assert all(session.closed for session in sessions)
    with report_mutex(world.tenant_id, report.pk, timeout_ms=1_000):
        assert sessions[-1].info.transaction_status == TransactionStatus.IDLE
    assert all(session.closed for session in sessions)


def test_bounded_acquisition_and_body_errors_release_every_mutex(
    world: World,
) -> None:
    report_id = uuid4()
    with (
        report_mutex(world.tenant_id, report_id),
        pytest.raises(ReportWireBusyError),
        report_mutex(world.tenant_id, report_id, timeout_ms=10),
    ):
        pytest.fail("a second session acquired an owned report")
    with pytest.raises(TimeoutError), report_mutex(world.tenant_id, report_id):
        raise TimeoutError
    with report_mutex(world.tenant_id, report_id, timeout_ms=1_000):
        pass


def test_process_death_releases_the_real_postgres_report_mutex(world: World) -> None:
    report_id = uuid4()
    parsed = urlsplit(os.environ["DATABASE_URL"])
    environment = dict(
        os.environ,
        DATABASE_URL=urlunsplit(
            parsed._replace(path=f"/{connection.settings_dict['NAME']}")
        ),
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import django; django.setup(); "
                "from tests.integrations.sitfis_mutex_process import main; main()"
            ),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(
            json.dumps({"tenant_id": str(world.tenant_id), "report_id": str(report_id)})
            + "\n"
        )
        process.stdin.flush()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=15), "mutex holder did not become ready"
        assert process.stdout.readline() == "SITFIS_REPORT_MUTEX_OWNED\n"
        with (
            pytest.raises(ReportWireBusyError),
            report_mutex(world.tenant_id, report_id, timeout_ms=10),
        ):
            pytest.fail("a live child did not own the expected database mutex")
        process.kill()
        process.communicate(timeout=10)
        with report_mutex(world.tenant_id, report_id, timeout_ms=5_000):
            pass
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
