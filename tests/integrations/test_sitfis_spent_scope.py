"""The review-six cuts enforce drain-before-exit on success and failure."""

import json
import sys

import pytest

from apps.integrations import call_unopened, provider_journal
from apps.integrations.journal_schema import JournalError, JournalRecord
from tests.integrations.sitfis_spent_scope import SpentAppend, markers
from tests.integrations.sitfis_support import clock, world
from tests.integrations.test_sitfis_unopened import Veto, veto

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "veto", "world"]


@pytest.mark.parametrize("cut", ["after-consume", "at-consume-return"])
@pytest.mark.parametrize("save_fails", [False, True])
def test_a_write_spent_at_exit_cannot_become_durable_after_issuing_admission_closes(
    veto: Veto, monkeypatch: pytest.MonkeyPatch, cut: str, save_fails: bool
) -> None:
    case = SpentAppend(veto, monkeypatch, cut, save_fails)
    case.run()
    assert case.exit_waiting.is_set(), (
        "admission exited before its active append drained"
    )
    assert case.observations == [{"issuer_closed_at_storage": False}]
    [inherited_and_record] = case.escaped
    inherited, record = inherited_and_record
    persisted = markers(veto)
    assert persisted == ([] if save_fails else [record])
    assert case.outcomes == (["rejected"] if save_fails else ["accepted"])
    stored = [
        row for row in persisted if case.observations[0]["issuer_closed_at_storage"]
    ]
    evidence = {
        "cut": cut,
        "save_fails": save_fails,
        "issuer_closed": case.issuer_closed.is_set(),
        "writer_outcomes": case.outcomes,
        "durable_markers_after_exit": len(stored),
        "physical_sends": len(veto.world.sent("/Emitir")),
        "observations": case.observations,
    }
    sys.stdout.write("RV6_DRAIN_ORDER " + json.dumps(evidence, sort_keys=True) + "\n")
    assert stored == [], (
        "a grant consumed before scope exit still wrote wire_veto "
        "after its issuing admission closed"
    )
    with pytest.raises(JournalError):
        inherited.run(case.real_append, record)
    with pytest.raises(JournalError):
        case.real_append(record)
    monkeypatch.setattr(provider_journal, "_save", case.real_save)
    assert isinstance(record, JournalRecord)
    with call_unopened._issue_unopened_write(record):
        assert case.real_append(record) == record.path
    assert markers(veto) == [record]
    assert call_unopened._UNOPENED_WRITES.get() is None
    assert veto.world.sent("/Emitir") == []
