# ruff: noqa: ANN401, E501, T201 - review 2 probes kept verbatim (oracle and RV2 trail lines)
"""AXRM-016 repair 2: review 2's probes committed with their oracle unchanged (R1a-c stale reply, R2 one-day cap, R3-R9)."""

import json
import logging
import threading
from typing import Any

import pytest
from django.db import connections

from apps.core.tenancy import tenant_context
from apps.integrations.call_models import ProviderCall
from tests.integrations.sitfis_support import (
    World,
    clock,
    processing_202,
    sitfis,
    world,
)
from tests.integrations.test_sitfis_stale_reply import _protocol, _slow_receive

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "world"]


def _ids(world: World, report: Any, label: str) -> dict[str, Any]:
    current = world.reload(report)
    with tenant_context(world.tenant_id):
        calls = [
            (str(c.pk), c.state, c.attempts, c.http_status, c.billed)
            for c in ProviderCall.objects.filter(operation="SP-SIT-R").order_by(
                "created_at"
            )
        ]
    out = {
        "label": label,
        "state": current.state,
        "emission_attempts": current.emission_attempts,
        "reply_call": current.reply_call,
        "reply_wait_ms": current.reply_wait_ms,
        "wait_left_s": (current.wait_until - world.clock.now()).total_seconds()
        if current.wait_until
        else None,
        "calls": calls,
        "emitir_requests": len(world.sent("/Emitir")),
    }
    print("RV2B", json.dumps(out, default=str), flush=True)
    return out


def test_r1c_stale_reply_ids_and_default_log(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    report = _protocol(world)
    world.clock.advance(seconds=30)
    world.script(processing_202(5_000))
    in_hand, go = _slow_receive(monkeypatch)
    mod = sitfis()
    outcome: dict[str, Any] = {}

    def runner_a() -> None:
        try:
            outcome["a"] = world.advance(report).state
        finally:
            connections.close_all()

    a = threading.Thread(target=runner_a)
    a.start()
    try:
        assert in_hand.wait(timeout=15)
        world.clock.advance(seconds=121)
        world.advance(report)
        _ids(world, report, "r1c-after-b")
        world.clock.advance(seconds=60)
        world.script(processing_202(300_000))
        real_perform = mod.perform
        seen: dict[str, Any] = {}

        def hold_after_final(*args: object, **kwargs: object) -> object:
            result = real_perform(*args, **kwargs)
            if threading.current_thread() is threading.main_thread():
                seen["c_final"] = _ids(world, report, "r1c-c-final-before-a")
                caplog.clear()
                caplog.set_level(logging.INFO, logger="apps.integrations.serpro.sitfis")
                go.set()
                a.join(timeout=30)
            return result

        monkeypatch.setattr(mod, "perform", hold_after_final)
        world.advance(report)
        monkeypatch.setattr(mod, "perform", real_perform)
    finally:
        go.set()
        a.join(timeout=30)
    assert not a.is_alive()
    after = _ids(world, report, "r1c-after-a-and-c")
    defaults = [
        getattr(r, "sitfis_event", "")
        for r in caplog.records
        if getattr(r, "sitfis_event", "") == "espera_padrao"
    ]
    print("RV2B_DEFAULT_WAIT_LOGS", json.dumps(defaults), flush=True)
    call1, call2 = after["calls"][0][0], after["calls"][1][0]
    print(
        "RV2B_IDS",
        json.dumps(
            {"call1": call1, "call2": call2, "reply_call_after": after["reply_call"]}
        ),
        flush=True,
    )
    assert after["wait_left_s"] == 300.0, after
    assert after["reply_call"] == call2, after
    assert after["reply_wait_ms"] == 300_000, after
