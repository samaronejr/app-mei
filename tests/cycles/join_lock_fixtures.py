"""Order real SQL in two runtime sessions; a timeout is never a race outcome."""

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from threading import Event
from typing import Literal, NamedTuple
from uuid import UUID

from django.db import DatabaseError, connection, connections, transaction
from psycopg import sql as pg
from psycopg.errors import DeadlockDetected, LockNotAvailable

from apps.core.tenancy import tenant_context
from apps.cycles.errors import VersionConflict
from tests.cycles.approval_fixtures import SIGNAL_TIMEOUT

CLIENT = "clients_clientcompany"
CYCLE = "cycles_servicecycle"
TENANT = "tenants_tenant"
FISCAL = "obligations_fiscaldocument"
type Action = Callable[[], UUID | str]
type Seam = Callable[..., object]
type Mode = Literal["KEY SHARE", "NO KEY UPDATE", "UPDATE"]


def locks(sql: str, table: str) -> bool:
    return f'FROM "{table}"' in sql and (
        "FOR UPDATE" in sql or "FOR NO KEY UPDATE" in sql or "FOR KEY SHARE" in sql
    )


def wait(event: Event) -> None:
    if not event.wait(SIGNAL_TIMEOUT):
        msg = "Lock-order harness did not reach the next SQL boundary."
        raise TimeoutError(msg)


@dataclass
class FirstLock:
    """If both writers want the same first row, give the left writer priority."""

    announced: Event = field(default_factory=Event)
    proceed: Event = field(default_factory=Event)
    held: Event = field(default_factory=Event)
    contender: Event = field(default_factory=Event)
    table: str = ""

    def left(self, execute: Seam, sql: str, *args: object) -> object:
        table = next((t for t in (CLIENT, CYCLE) if locks(sql, t)), "")
        if not table or self.table:
            return execute(sql, *args)
        self.table = table
        self.announced.set()
        wait(self.proceed)
        result = execute(sql, *args)
        self.held.set()
        wait(self.contender)
        return result

    def right(self, execute: Seam, sql: str, *args: object) -> object:
        if locks(sql, self.table):
            self.proceed.set()
            wait(self.held)
            self.contender.set()
        elif not self.proceed.is_set() and any(locks(sql, t) for t in (CLIENT, CYCLE)):
            result = execute(sql, *args)
            self.proceed.set()
            wait(self.held)
            return result
        return execute(sql, *args)


class Result(NamedTuple):
    pid: int
    status: str
    value: UUID | str | None


def worker(tenant: UUID | None, seam: Seam, action: Action) -> Result:
    try:
        with connection.cursor() as cur:
            cur.execute("SET ROLE app_runtime")
            cur.execute("SET statement_timeout = '25s'")
            cur.execute("SELECT pg_backend_pid(), current_user")
            pid, role = cur.fetchone()
            assert role == "app_runtime"
        try:
            with (
                connection.execute_wrapper(seam),
                tenant_context(tenant) if tenant else nullcontext(),
            ):
                value = action()
        except VersionConflict:
            return Result(pid, "VersionConflict", None)
        except DatabaseError as exc:
            if not isinstance(exc.__cause__, DeadlockDetected):
                raise
            return Result(pid, "40P01", None)
        return Result(pid, "committed", value)
    finally:
        connections.close_all()


def ordered(
    tenant: UUID, first: Action, second: Action, *, phased_second: bool = False
) -> tuple[Result, Result]:
    gate = FirstLock()
    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(worker, tenant, gate.left, first)
        try:
            wait(gate.announced)
            right = pool.submit(
                worker, None if phased_second else tenant, gate.right, second
            )
            a, b = left.result(2 * SIGNAL_TIMEOUT), right.result(2 * SIGNAL_TIMEOUT)
        finally:
            gate.proceed.set()
            gate.held.set()
            gate.contender.set()
    assert a.pid != b.pid
    return a, b


def available(table: str, key: UUID, mode: Mode) -> bool:
    """Rollback a successful probe too: it must leave no locks behind."""
    try:
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(
                pg.SQL("SELECT id FROM {} WHERE id=%s FOR {} NOWAIT").format(
                    pg.Identifier(table), pg.SQL(mode)
                ),
                [key],
            )
            assert cur.fetchone() == (key,)
            transaction.set_rollback(True)
    except DatabaseError as exc:
        if not isinstance(exc.__cause__, LockNotAvailable):
            raise
        return False
    return True


@contextmanager
def paused_client(tenant: UUID | None, action: Action, next_row: str) -> Iterator[None]:
    entered, release = Event(), Event()

    def seam(execute: Seam, sql: str, *args: object) -> object:
        if not entered.is_set() and locks(sql, next_row):
            # Still probe the database if a mutant omitted the client fence.
            entered.set()
            wait(release)
        result = execute(sql, *args)
        if not entered.is_set() and locks(sql, CLIENT):
            entered.set()
            wait(release)
        return result

    with ThreadPoolExecutor(max_workers=1) as pool:
        writing = pool.submit(worker, tenant, seam, action)
        try:
            wait(entered)
            yield
        finally:
            release.set()
        assert writing.result(SIGNAL_TIMEOUT).status == "committed"


def probe(tenant: UUID, inspect: Callable[[], None]) -> None:
    """Independent observer; never reuse the fixture session or writer backend."""

    def run() -> None:
        try:
            with connection.cursor() as cur:
                cur.execute("SET ROLE app_runtime")
                cur.execute("SELECT current_user")
                assert cur.fetchone() == ("app_runtime",)
            with tenant_context(tenant):
                inspect()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(run).result(SIGNAL_TIMEOUT)
