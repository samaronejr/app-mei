"""Review six: a consumed writer grant cannot outlive its admission."""

import json
import sys
import threading
from collections.abc import Callable
from contextvars import copy_context

import pytest
from django.db import connections

from apps.integrations import provider_journal
from apps.integrations.call_unopened import is_unopened
from apps.integrations.journal_schema import (
    FirmJournalRecord,
    JournalError,
    JournalRecord,
)
from tests.integrations.sitfis_support import clock, world
from tests.integrations.test_sitfis_unopened import CrashCutError, Veto, veto

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "veto", "world"]


def markers(case: Veto) -> list[JournalRecord]:
    return [
        row
        for row in provider_journal.read(case.world.tenant_id)
        if is_unopened(row.operation, row.result)
    ]


def _append_in_thread(
    append: Callable[[], str], outcomes: list[str], done: threading.Event
) -> None:
    try:
        append()
        outcomes.append("accepted")
    except JournalError:
        outcomes.append("rejected")
    finally:
        connections.close_all()
        done.set()


@pytest.mark.parametrize("cut", ["after-consume", "at-consume-return"])
def test_a_write_spent_at_exit_cannot_become_durable_after_issuing_admission_closes(
    veto: Veto, monkeypatch: pytest.MonkeyPatch, cut: str
) -> None:
    real_append = provider_journal.append
    real_save = provider_journal._save
    real_consume = provider_journal.consume_unopened_write
    spent, release, done, issuer_closed = (threading.Event() for _ in range(4))
    threads: list[threading.Thread] = []
    outcomes: list[str] = []
    observations: list[dict[str, bool]] = []

    def pause() -> None:
        spent.set()
        assert release.wait(timeout=20), "writer was never released"

    def held_save(path: str, raw: bytes) -> None:
        if (
            threading.current_thread().name == "rv6-spent-thread"
            and cut == "after-consume"
        ):
            pause()
        if threading.current_thread().name == "rv6-spent-thread":
            observations.append({"issuer_closed_at_storage": issuer_closed.is_set()})
        return real_save(path, raw)

    def held_consume(record: JournalRecord | FirmJournalRecord) -> None:
        real_consume(record)
        if (
            threading.current_thread().name == "rv6-spent-thread"
            and is_unopened(record.operation, record.result)
            and cut == "at-consume-return"
        ):
            pause()

    def interrupt(record: JournalRecord | FirmJournalRecord) -> str:
        if not is_unopened(record.operation, record.result):
            return real_append(record)
        inherited = copy_context()

        thread = threading.Thread(
            target=_append_in_thread,
            args=(lambda: inherited.run(real_append, record), outcomes, done),
            name="rv6-spent-thread",
        )
        threads.append(thread)
        thread.start()
        assert spent.wait(timeout=15), "thread never consumed the grant"
        raise CrashCutError

    monkeypatch.setattr(provider_journal, "_save", held_save)
    monkeypatch.setattr(provider_journal, "consume_unopened_write", held_consume)
    monkeypatch.setattr(provider_journal, "append", interrupt)
    try:
        with pytest.raises(CrashCutError):
            veto.run()
        issuer_closed.set()
        assert markers(veto) == [], (
            "marker was already durable before releasing the writer"
        )
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=25)
    assert done.is_set()
    stored = markers(veto)
    evidence = {
        "cut": cut,
        "issuer_closed": issuer_closed.is_set(),
        "writer_outcomes": outcomes,
        "durable_markers": len(stored),
        "physical_sends": len(veto.world.sent("/Emitir")),
        "observations": observations,
    }
    sys.stdout.write(
        "RV6_SPENT_AT_SCOPE_EXIT " + json.dumps(evidence, sort_keys=True) + "\n"
    )
    assert stored == [], (
        "a grant consumed before scope exit still wrote wire_veto "
        "after its issuing admission closed"
    )
