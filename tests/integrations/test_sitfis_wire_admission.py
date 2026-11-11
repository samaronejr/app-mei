"""Review 4's real-wire oracle across the dispatch-start/transmit gap."""

import json
import sys
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from django.db import connections

from apps.core.tenancy import tenant_context
from apps.integrations import call_protocol
from apps.integrations.call_contracts import CallResult
from apps.integrations.call_dispatch import Adapter, Dispatch, transmit
from apps.obligations.models import Document, FiscalSituationReport
from tests.integrations.sitfis_support import (
    World,
    clock,
    processing_202,
    processing_204,
    protocol_reply,
    report_reply,
    sitfis,
    world,
)

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "world"]


def _pause_reply[**P, T](
    callback: Callable[P, T], ready: threading.Event, go: threading.Event
) -> Callable[P, T]:
    def held(*args: P.args, **kwargs: P.kwargs) -> T:
        if threading.current_thread() is not threading.main_thread():
            ready.set()
            assert go.wait(timeout=30)
        return callback(*args, **kwargs)

    return held


def _committed_reply(
    world: World,
    report: FiscalSituationReport,
    reply_kind: str,
    original_received: datetime,
) -> dict[str, object]:
    before_wire = world.reload(report)
    with tenant_context(world.tenant_id):
        documents = Document.objects.filter(
            acquisition_origin="provedor_federal"
        ).count()
    observed: dict[str, object] = {
        "kind": reply_kind,
        "send_time": world.clock.now().isoformat(),
        "announced_deadline": (original_received + timedelta(seconds=300)).isoformat(),
        "receipts": before_wire.call_replies,
        "state": before_wire.state,
        "document": str(before_wire.document_id),
        "durable_documents": documents,
        "sends_before": len(world.sent("/Emitir")),
    }
    if reply_kind == "pdf":
        assert documents == 1
        assert before_wire.document_id is not None
    else:
        assert world.clock.now() < original_received + timedelta(seconds=300)
        assert any(
            receipt["wait_ms"] == 300_000
            for values in before_wire.call_replies.values()
            for receipt in values
        )
    return observed


def _old_runner(world: World, report: FiscalSituationReport, errors: list[str]) -> None:
    try:
        world.advance(report)
    except BaseException as error:
        errors.append(repr(error))
        raise
    finally:
        connections.close_all()


@pytest.mark.parametrize("reply_kind", ["202", "204", "pdf"])
def test_a_durable_reply_before_the_next_wire_prevents_early_or_duplicate_payment(
    world: World, monkeypatch: pytest.MonkeyPatch, reply_kind: str
) -> None:
    mod = sitfis()
    world.script(protocol_reply(30_000))
    report = world.advance(world.request())
    world.clock.advance(seconds=30)
    original_received = world.clock.now()
    replies = {
        "202": processing_202(300_000),
        "204": processing_204(300_000),
        "pdf": report_reply(),
    }
    world.script(replies[reply_kind])
    ready, go = threading.Event(), threading.Event()
    errors: list[str] = []

    hook = "_receive" if reply_kind != "pdf" else "ingest_system_document"
    monkeypatch.setattr(mod, hook, _pause_reply(getattr(mod, hook), ready, go))

    runner = threading.Thread(target=_old_runner, args=(world, report, errors))
    runner.start()
    try:
        assert ready.wait(timeout=15)
        world.clock.advance(seconds=121)
        waiting = world.advance(report)
        assert waiting.emission_attempts == 1
        world.clock.advance(seconds=60)
        real_transmit = transmit
        checked: list[dict[str, object]] = []

        def late_receipt_before_actual_wire(
            dispatch: Dispatch, adapter: Adapter
        ) -> CallResult:
            # Dispatch-start committed, but the second request has sent no bytes.
            assert len(world.sent("/Emitir")) == 1
            [_, current] = world.calls("SP-SIT-R")
            assert current.state == "despacho_iniciado"
            go.set()
            runner.join(timeout=15)
            assert not runner.is_alive()
            assert errors == []
            checked.append(
                _committed_reply(world, report, reply_kind, original_received)
            )
            return real_transmit(dispatch, adapter)

        monkeypatch.setattr(call_protocol, "transmit", late_receipt_before_actual_wire)
        world.script(report_reply())
        final = world.advance(report)
        sys.stdout.write(
            "RV4_POST_COMMIT "
            + json.dumps(
                {
                    "checked": checked,
                    "sends_after": len(world.sent("/Emitir")),
                    "final": final.state,
                }
            )
            + "\n"
        )
        sys.stdout.flush()
        assert checked
        assert len(world.sent("/Emitir")) == 1, checked
    finally:
        go.set()
        runner.join(timeout=30)
    assert not runner.is_alive()
    assert errors == []
