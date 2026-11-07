"""Exhaust legal consume/exit/append orders against the real journal writer."""

import json
import os
import select
import sys
from contextlib import ExitStack
from contextvars import Context, copy_context
from dataclasses import dataclass, field, replace
from itertools import permutations, product
from tempfile import TemporaryDirectory
from threading import Event, Thread, active_count, get_ident
from unittest.mock import patch
from uuid import UUID, uuid4

from django.core.files.storage import default_storage
from django.test import override_settings

from apps.integrations import call_unopened, provider_journal
from apps.integrations.journal_schema import JournalError, JournalRecord, decode
from tests.integrations.sitfis_scope_lock_probe import ScopeLockProbe

ORDERS = tuple(
    order
    for order in permutations(("consume", "exit", "append"))
    if order.index("consume") < order.index("append")
)


def wait(event: Event) -> None:
    assert event.wait(timeout=10), "ordering actor failed to reach its barrier"


@dataclass
class ThreadOrder:
    record: JournalRecord
    mode: str
    save_fails: bool
    prelock: bool = False
    ready: Event = field(default_factory=Event)
    start: Event = field(default_factory=Event)
    consumed_or_done: Event = field(default_factory=Event)
    allow_append: Event = field(default_factory=Event)
    request_exit: Event = field(default_factory=Event)
    exit_progress: Event = field(default_factory=Event)
    exit_waiting: Event = field(default_factory=Event)
    exited: Event = field(default_factory=Event)
    done: Event = field(default_factory=Event)
    before_lock: Event = field(default_factory=Event)
    allow_lock: Event = field(default_factory=Event)
    context: Context | None = None
    trace: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    outcome: str = ""

    def owner(self) -> None:
        try:
            with (
                ExitStack() as patches,
                call_unopened._issue_unopened_write(self.record),
            ):
                scope = call_unopened._UNOPENED_WRITES.get()
                assert scope is not None
                observer = ScopeLockProbe(
                    scope.lock,
                    get_ident(),
                    self.exit_progress,
                    self.exit_waiting,
                    self.before_lock if self.prelock else None,
                    self.allow_lock if self.prelock else None,
                )
                patches.enter_context(patch.object(scope, "lock", observer))
                snapshot = copy_context()
                self.context = (
                    snapshot.copy() if self.mode == "copy_context" else snapshot
                )
                self.ready.set()
                wait(self.request_exit)
            self.trace.append("exit")
        except BaseException as error:
            self.errors.append(repr(error))
            raise
        finally:
            self.ready.set()
            self.exited.set()
            self.exit_progress.set()

    def writer(self) -> None:
        try:
            wait(self.start)
            assert self.context is not None
            self.context.run(provider_journal.append, self.record)
            self.outcome = "accepted"
        except JournalError:
            self.outcome = "rejected"
        except BaseException as error:
            self.errors.append(repr(error))
            raise
        finally:
            self.done.set()
            self.consumed_or_done.set()

    def step(self, action: str) -> None:
        if action == "consume":
            self.start.set()
            wait(self.consumed_or_done)
        elif action == "exit":
            self.request_exit.set()
            wait(self.exit_progress)
        else:
            self.allow_append.set()
            wait(self.done)

    def execute(self, order: tuple[str, ...]) -> None:
        owner = Thread(target=self.owner, name="scope-owner", daemon=True)
        writer = Thread(target=self.writer, name="scope-writer", daemon=True)
        owner.start()
        wait(self.ready)
        writer.start()
        try:
            if self.prelock:
                self.start.set()
                wait(self.before_lock)
                self.request_exit.set()
                wait(self.exited)
                self.allow_lock.set()
                self.allow_append.set()
            else:
                for action in order:
                    self.step(action)
            wait(self.done)
            wait(self.exited)
        finally:
            for event in (
                self.start,
                self.request_exit,
                self.allow_append,
                self.allow_lock,
            ):
                event.set()
            owner.join(timeout=10)
            writer.join(timeout=10)
        assert not owner.is_alive(), "scope owner deadlocked"
        assert not writer.is_alive(), "scope writer deadlocked"
        assert self.errors == []


def thread_order(
    record: JournalRecord,
    order: tuple[str, ...],
    mode: str,
    fails: bool,
    *,
    prelock: bool = False,
) -> dict[str, object]:
    case = ThreadOrder(record, mode, fails, prelock)
    consume = call_unopened.consume_unopened_write
    save = provider_journal._save

    def observed_consume(value: JournalRecord) -> None:
        consume(value)
        case.trace.append("consume")
        case.consumed_or_done.set()

    def held_save(path: str, raw: bytes) -> None:
        wait(case.allow_append)
        if fails:
            case.trace.append("save_failed")
            message = "synthetic pre-persistence storage failure"
            raise OSError(message)
        save(path, raw)
        case.trace.append("append")

    with (
        patch.object(provider_journal, "consume_unopened_write", observed_consume),
        patch.object(provider_journal, "_save", held_save),
    ):
        case.execute(order)
    expected = "rejected" if prelock or order[0] == "exit" or fails else "accepted"
    assert case.outcome == expected, (order, case.trace, case.outcome)
    assert default_storage.exists(record.path) == (expected == "accepted")
    for event in ("append", "save_failed"):
        if event in case.trace:
            assert case.trace.index(event) < case.trace.index("exit"), case.trace
    if not prelock and order == ("consume", "exit", "append"):
        assert case.exit_waiting.is_set(), "exit did not drain its active save"
    return {
        "mode": mode,
        "requested_order": order,
        "save_fails": fails,
        "prelock": prelock,
        "observed": case.trace,
        "outcome": case.outcome,
    }


def read_signal(descriptor: int) -> bytes:
    readable, _, _ = select.select([descriptor], [], [], 10)
    assert readable, "fork actor failed to reach its barrier"
    return os.read(descriptor, 1)


def fork_writer(record: JournalRecord, command: int, reply: int, fails: bool) -> None:
    consume = call_unopened.consume_unopened_write
    save = provider_journal._save

    def observed_consume(value: JournalRecord) -> None:
        consume(value)
        os.write(reply, b"C")

    def held_save(path: str, raw: bytes) -> None:
        assert read_signal(command) == b"A"
        if fails:
            message = "synthetic child storage failure"
            raise OSError(message)
        save(path, raw)

    try:
        assert read_signal(command) == b"C"
        with (
            patch.object(provider_journal, "consume_unopened_write", observed_consume),
            patch.object(provider_journal, "_save", held_save),
        ):
            provider_journal.append(record)
        os.write(reply, b"S")
        os._exit(0)
    except JournalError:
        os.write(reply, b"R")
        os._exit(0)


def fork_order(
    record: JournalRecord, order: tuple[str, ...], fails: bool
) -> dict[str, object]:
    assert active_count() == 1
    child_read, parent_write = os.pipe()
    parent_read, child_write = os.pipe()
    scope = call_unopened._issue_unopened_write(record)
    scope.__enter__()
    closed = False
    response = b""
    trace = []
    pid = os.fork()
    if pid == 0:
        os.close(parent_write)
        os.close(parent_read)
        fork_writer(record, child_read, child_write, fails)
    os.close(child_read)
    os.close(child_write)
    try:
        for action in order:
            if action == "consume":
                os.write(parent_write, b"C")
                response = read_signal(parent_read)
                trace.append("consume" if response == b"C" else "rejected")
            elif action == "exit":
                scope.__exit__(None, None, None)
                closed = True
                trace.append("exit")
            elif response == b"C":
                os.write(parent_write, b"A")
                response = read_signal(parent_read)
                trace.append("append" if response == b"S" else "rejected")
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        assert response == b"R", trace
        assert "consume" not in trace, "fork consumed its parent's grant"
        assert not default_storage.exists(record.path), trace
    finally:
        if not closed:
            scope.__exit__(None, None, None)
        os.close(parent_write)
        os.close(parent_read)
    return {
        "mode": "fork",
        "requested_order": order,
        "save_fails": fails,
        "observed": trace,
        "outcome": "rejected",
    }


def main() -> None:
    raw = sys.stdin.buffer.read()
    base = decode(raw, UUID(json.loads(raw)["tenant_id"]))
    checked = []
    with (
        TemporaryDirectory() as directory,
        override_settings(
            STORAGES={
                "default": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage",
                    "OPTIONS": {"location": directory},
                }
            }
        ),
    ):
        for mode in ("thread", "copy_context"):
            result = thread_order(
                replace(base, attempt_id=uuid4()),
                ("exit", "consume", "append"),
                mode,
                fails=False,
                prelock=True,
            )
            checked.append(result)
            sys.stdout.write("SCOPE_ORDER " + json.dumps(result, sort_keys=True) + "\n")
        for mode, order, fails in product(
            ("thread", "copy_context", "fork"), ORDERS, (False, True)
        ):
            record = replace(base, attempt_id=uuid4())
            result = (
                fork_order(record, order, fails)
                if mode == "fork"
                else thread_order(record, order, mode, fails)
            )
            checked.append(result)
            sys.stdout.write("SCOPE_ORDER " + json.dumps(result, sort_keys=True) + "\n")
            control = replace(base, attempt_id=uuid4())
            with call_unopened._issue_unopened_write(control):
                assert provider_journal.append(control) == control.path
            assert default_storage.exists(control.path)
    assert len(ORDERS) == 3
    assert len(checked) == 20
    sys.stdout.write("SCOPE_ORDERINGS_CHECKED=20\n")
