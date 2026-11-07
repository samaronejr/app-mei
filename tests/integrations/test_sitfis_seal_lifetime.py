"""Review five: an unspent copied writer grant expires with its issuer."""

from contextvars import Context, copy_context

import pytest

from apps.integrations import call_dispatch, provider_journal
from apps.integrations.call_unopened import UNOPENED_RESULT, is_unopened
from apps.integrations.journal_schema import FirmJournalRecord, JournalError, JournalRecord
from apps.integrations.transport_types import EnvioNaoAutorizado
from tests.integrations.sitfis_support import clock, world
from tests.integrations.test_sitfis_unopened import CrashCutError, Veto, veto

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "veto", "world"]


def test_a_copied_writer_context_cannot_publish_after_the_issuer_has_closed(
    veto: Veto, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_append = provider_journal.append
    escaped: list[tuple[Context, JournalRecord | FirmJournalRecord]] = []

    def interrupted(record: JournalRecord | FirmJournalRecord) -> str:
        if is_unopened(record.operation, record.result):
            escaped.append((copy_context(), record))
            raise CrashCutError
        return original_append(record)

    monkeypatch.setattr(provider_journal, "append", interrupted)
    with pytest.raises(CrashCutError):
        veto.run()
    [dispatch] = veto.dispatches
    [context_and_record] = escaped
    inherited, record = context_and_record
    # The actual dispatch/window has ended; neither public API has current rights.
    with pytest.raises(EnvioNaoAutorizado):
        call_dispatch.record_result(dispatch, UNOPENED_RESULT)
    with pytest.raises(JournalError):
        original_append(record)
    accepted = False
    try:
        inherited.run(original_append, record)
        accepted = True
    except JournalError:
        pass
    stored = [
        row
        for row in provider_journal.read(veto.world.tenant_id)
        if is_unopened(row.operation, row.result)
    ]
    print(
        "RV5_ESCAPED_WRITER",
        {
            "issuer_closed": True,
            "copied_context_accepted": accepted,
            "durable_markers": len(stored),
            "sends": len(veto.world.sent("/Emitir")),
        },
        flush=True,
    )
    assert accepted is False, (
        "copied context retained a live marker capability after its issuer closed"
    )
    assert stored == []
