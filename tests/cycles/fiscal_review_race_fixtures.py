"""Order two review writers at the existing client SELECT FOR UPDATE seam."""

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
from typing import TYPE_CHECKING

from django.db import connection, connections

from apps.clients.models import ClientCompany
from tests.cycles.approval_fixtures import SIGNAL_TIMEOUT, LockSignal

if TYPE_CHECKING:
    from collections.abc import Callable


def review_race[T](
    first: "Callable[[], T]",
    second: "Callable[[], T]",
) -> tuple[Future[T], Future[T]]:
    held, release, contending = Event(), Event(), Event()

    def run(call: "Callable[[], T]", seam: LockSignal) -> T:
        try:
            with connection.execute_wrapper(seam):
                return call()
        finally:
            connections.close_all()

    table = ClientCompany._meta.db_table
    with ThreadPoolExecutor(max_workers=2) as executor:
        signal = LockSignal(table, held, release, after=True)
        winner = executor.submit(run, first, signal)
        try:
            assert held.wait(SIGNAL_TIMEOUT)
            loser = executor.submit(run, second, LockSignal(table, contending))
            assert contending.wait(SIGNAL_TIMEOUT)
        finally:
            release.set()
    return winner, loser
