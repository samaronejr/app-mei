"""Schedule the real spent-marker writer and its issuing admission."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import Context, copy_context
from dataclasses import dataclass, field
from threading import Event, Thread, current_thread, get_ident

import pytest
from django.db import connections

from apps.integrations import call_dispatch, call_unopened, provider_journal
from apps.integrations.call_unopened import is_unopened
from apps.integrations.journal_schema import (
    FirmJournalRecord,
    JournalError,
    JournalRecord,
)
from tests.integrations.sitfis_scope_lock_probe import ScopeLockProbe
from tests.integrations.test_sitfis_unopened import CrashCutError, Veto

Record = JournalRecord | FirmJournalRecord


def markers(case: Veto) -> list[JournalRecord]:
    return [
        row
        for row in provider_journal.read(case.world.tenant_id)
        if is_unopened(row.operation, row.result)
    ]


@dataclass
class SpentAppend:
    case: Veto
    patch: pytest.MonkeyPatch
    cut: str
    save_fails: bool
    spent: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)
    done: Event = field(default_factory=Event)
    issuer_closed: Event = field(default_factory=Event)
    exit_progress: Event = field(default_factory=Event)
    exit_waiting: Event = field(default_factory=Event)
    threads: list[Thread] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)
    observations: list[dict[str, bool]] = field(default_factory=list)
    escaped: list[tuple[Context, Record]] = field(default_factory=list)
    observer_errors: list[BaseException] = field(default_factory=list)
    real_append: Callable[[Record], str] = field(init=False)
    real_save: Callable[[str, bytes], None] = field(init=False)
    real_consume: Callable[[Record], None] = field(init=False)

    def __post_init__(self) -> None:
        self.real_append = provider_journal.append
        self.real_save = provider_journal._save
        self.real_consume = call_unopened.consume_unopened_write

    @contextmanager
    def observed_issue(self, record: JournalRecord) -> Iterator[None]:
        with call_unopened._issue_unopened_write(record):
            scope = call_unopened._UNOPENED_WRITES.get()
            assert scope is not None
            # The rejected product had no lock; the ordering oracle still applies.
            if hasattr(scope, "lock"):
                self.patch.setattr(
                    scope,
                    "lock",
                    ScopeLockProbe(
                        scope.lock, get_ident(), self.exit_progress, self.exit_waiting
                    ),
                )
            yield

    def release_during_exit(self) -> None:
        try:
            assert self.exit_progress.wait(timeout=15)
            assert markers(self.case) == [], (
                "marker was already durable before releasing the writer"
            )
        except BaseException as error:
            self.observer_errors.append(error)
            raise
        finally:
            self.release.set()

    def pause(self) -> None:
        self.spent.set()
        assert self.release.wait(timeout=20), "writer was never released"

    def held_save(self, path: str, raw: bytes) -> None:
        if current_thread().name == "rv6-spent-thread" and self.cut == "after-consume":
            self.pause()
        if current_thread().name == "rv6-spent-thread":
            self.observations.append(
                {"issuer_closed_at_storage": self.issuer_closed.is_set()}
            )
            if self.save_fails:
                message = "synthetic storage failure before persistence"
                raise OSError(message)
        self.real_save(path, raw)

    def held_consume(self, record: Record) -> None:
        self.real_consume(record)
        if (
            current_thread().name == "rv6-spent-thread"
            and is_unopened(record.operation, record.result)
            and self.cut == "at-consume-return"
        ):
            self.pause()

    def write(self, inherited: Context, record: Record) -> None:
        try:
            inherited.run(self.real_append, record)
            self.outcomes.append("accepted")
        except JournalError:
            self.outcomes.append("rejected")
        finally:
            connections.close_all()
            self.done.set()

    def interrupt(self, record: Record) -> str:
        if not is_unopened(record.operation, record.result):
            return self.real_append(record)
        inherited = copy_context()
        self.escaped.append((inherited, record))
        thread = Thread(
            target=self.write, args=(inherited, record), name="rv6-spent-thread"
        )
        self.threads.append(thread)
        thread.start()
        assert self.spent.wait(timeout=15), "thread never consumed the grant"
        raise CrashCutError

    def run(self) -> None:
        self.patch.setattr(provider_journal, "_save", self.held_save)
        self.patch.setattr(
            provider_journal, "consume_unopened_write", self.held_consume
        )
        self.patch.setattr(provider_journal, "append", self.interrupt)
        self.patch.setattr(call_dispatch, "_issue_unopened_write", self.observed_issue)
        observer = Thread(target=self.release_during_exit, name="rv6-exit-observer")
        observer.start()
        try:
            with pytest.raises(CrashCutError):
                self.case.run()
        finally:
            self.issuer_closed.set()
            self.exit_progress.set()
            observer.join(timeout=20)
            for thread in self.threads:
                thread.join(timeout=25)
        assert not observer.is_alive()
        assert all(not thread.is_alive() for thread in self.threads)
        assert self.observer_errors == []
        assert self.done.is_set()
