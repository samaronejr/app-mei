"""Review 3's held-reply witnesses, with the original wire-count oracle."""

import json
import threading
from typing import Any

import pytest
from django.db import connections

from apps.core.tenancy import tenant_context
from apps.obligations.models import Document, FiscalSituationReport
from tests.fakes.provider import Reply
from tests.integrations.sitfis_support import (
    World,
    clock,
    envelope,
    processing_202,
    processing_204,
    protocol_reply,
    report_reply,
    sitfis,
    world,
)

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "world"]


def _reply(kind: str) -> Reply:
    if kind.startswith("pdf-"):
        return report_reply()
    if kind == "late-one-day":
        return processing_202(3 * 86_400_000)
    if kind == "late-absent":
        return Reply(status=202, body=envelope(202, "{}"))
    return processing_202(300_000)


def _intervene(
    world: World, report: FiscalSituationReport, kind: str
) -> dict[str, Any]:
    with tenant_context(world.tenant_id):
        durable = Document.objects.filter(acquisition_origin="provedor_federal").count()
    world.clock.advance(seconds=121)
    outcome = world.advance(report).state
    if kind == "pdf-after-newer-wait":
        world.clock.advance(seconds=60)
        world.script(processing_202(300_000))
        world.advance(report)
    elif kind == "pdf-after-limit":
        world.clock.advance(seconds=60)
        world.script(processing_202(0), processing_202(0))
        world.advance(report)
    before = world.reload(report)
    assert before.wait_until is not None
    return {
        "case": kind,
        "durable_before_release": durable,
        "before_state": before.state,
        "before_attempts": before.emission_attempts,
        "before_wait_s": (before.wait_until - world.clock.now()).total_seconds(),
        "runner_b": outcome,
    }


def _assert_outcome(world: World, observed: dict[str, Any]) -> None:
    kind = observed["case"]
    if kind.startswith("pdf-"):
        assert observed["final_state"] == "emitido", observed
        assert observed["after_document"] != "None", observed
        assert observed["documents"] == 1, observed
    elif kind == "late-absent":
        assert len(world.sent("/Emitir")) == 2, observed
        assert observed["final_state"] == "emitido", observed
    else:
        assert len(world.sent("/Emitir")) == 1, observed


@pytest.mark.parametrize(
    "kind",
    [
        "late-202",
        "late-one-day",
        "pdf-after-limit",
        "late-absent",
        "pdf-after-newer-wait",
    ],
)
def test_review3_held_reply(
    world: World, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Keep the reply barrier and wire counts from the executed review probe."""
    mod = sitfis()
    world.script(protocol_reply(30_000))
    report = world.advance(world.request())
    world.clock.advance(seconds=30)
    world.script(_reply(kind))
    in_hand, go = threading.Event(), threading.Event()
    real_receive = mod._receive
    errors: list[str] = []
    outcomes: dict[str, Any] = {}

    def held_receive(*args: Any) -> None:  # noqa: ANN401 - original probe barrier
        if threading.current_thread() is not threading.main_thread():
            in_hand.set()
            assert go.wait(timeout=30), "reviewer release did not arrive"
        real_receive(*args)

    monkeypatch.setattr(mod, "_receive", held_receive)

    def runner_a() -> None:
        try:
            outcomes["a"] = world.advance(report).state
        except BaseException as error:  # noqa: BLE001 - report thread failures
            errors.append(repr(error))
        finally:
            connections.close_all()

    a = threading.Thread(target=runner_a)
    a.start()
    try:
        assert in_hand.wait(timeout=15), "runner did not reach reply recording"
        observed = _intervene(world, report, kind)
    finally:
        go.set()
        a.join(timeout=30)
    assert not a.is_alive(), "reply runner is still alive"
    assert errors == [], errors
    monkeypatch.setattr(mod, "_receive", real_receive)
    after = world.advance(report)
    assert after.wait_until is not None
    sends_after_reply = len(world.sent("/Emitir"))
    if not kind.startswith("pdf-"):
        world.clock.advance(seconds=60)
        world.script(report_reply())
        final = world.advance(report)
    else:
        final = after
    with tenant_context(world.tenant_id):
        documents = Document.objects.filter(
            acquisition_origin="provedor_federal"
        ).count()
    observed.update(
        {
            "after_state": after.state,
            "after_wait_s": (after.wait_until - world.clock.now()).total_seconds(),
            "after_document": str(after.document_id),
            "sends_after_reply": sends_after_reply,
            "sends_at_plus_60": len(world.sent("/Emitir")),
            "final_state": final.state,
            "documents": documents,
            "outcomes": outcomes,
        }
    )
    print("RV3_CASE", json.dumps(observed), flush=True)  # noqa: T201
    _assert_outcome(world, observed)


@pytest.mark.parametrize("status", [202, 204])
def test_the_receipt_keeps_wire_receive_time_after_a_pause_before_recording(
    world: World, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    mod = sitfis()
    world.script(protocol_reply(30_000))
    report = world.advance(world.request())
    world.clock.advance(seconds=30)
    received_at = world.clock.now()
    reply = processing_202(300_000) if status == 202 else processing_204(300_000)
    world.script(reply)
    real_receive = mod._receive

    def paused(*args: Any) -> None:  # noqa: ANN401 - receive boundary
        world.clock.advance(seconds=121)
        real_receive(*args)

    monkeypatch.setattr(mod, "_receive", paused)
    world.advance(report)
    [call] = world.calls("SP-SIT-R")
    receipts = world.reload(report).call_replies[str(call.pk)]
    assert receipts == [{"received_at": received_at.isoformat(), "wait_ms": 300_000}]
    assert len(world.sent("/Emitir")) == 1
