# ruff: noqa: ANN401, E501, EM101, PT006, PT018, T201, TRY003 - review 2 probes kept verbatim (oracle and RV2 trail lines)
"""AXRM-016 repair 2: review 2's probes committed with their oracle unchanged (R1a-c stale reply, R2 one-day cap, R3-R9)."""

import json
import threading
from typing import Any

import pytest
from django.conf import settings as django_settings
from django.db import connections

from apps.core.tenancy import tenant_context
from apps.cycles.models import Exception as CycleException
from apps.integrations.call_models import ProviderCall
from apps.integrations.models import IntegrationIssue
from apps.obligations.models import Document
from tests.fakes.provider import Reply
from tests.integrations.sitfis_support import (
    World,
    clock,
    module,
    processing_202,
    processing_204,
    protocol_reply,
    report_reply,
    sitfis,
    world,
)

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "world"]


def snap(world: World, report: Any, label: str) -> dict[str, Any]:
    mod = sitfis()
    current = world.reload(report)
    with tenant_context(world.tenant_id):
        calls = [
            (c.operation, str(c.pk)[:8], c.state, c.attempts, c.http_status, c.billed)
            for c in ProviderCall.objects.order_by("created_at")
        ]
        problems = list(
            CycleException.objects.filter(
                reason_code="situacao_fiscal_nao_obtida"
            ).values_list("state", flat=True)
        )
        issues = sorted(IntegrationIssue.objects.values_list("reason", "service"))
        documents = Document.objects.filter(
            acquisition_origin="provedor_federal"
        ).count()
    budget = world.budget()
    nxt = mod.next_run_at(current)
    out = {
        "label": label,
        "state": current.state,
        "emission_attempts": current.emission_attempts,
        "reply_call": (current.reply_call or "")[:8],
        "reply_wait_ms": current.reply_wait_ms,
        "wait_until_minus_now_s": (
            (current.wait_until - world.clock.now()).total_seconds()
            if current.wait_until
            else None
        ),
        "next_run_in_s": (nxt - world.clock.now()).total_seconds() if nxt else None,
        "calls": calls,
        "report_problems": problems,
        "issues": issues,
        "documents": documents,
        "reserved": budget.reserved,
        "known_liability": budget.known_liability,
        "emitir_requests": len(world.sent("/Emitir")),
        "apoiar_requests": len(world.sent("/Apoiar")),
    }
    print("RV2", json.dumps(out, default=str), flush=True)
    return out


def _protocol(world: World, wait_ms: int = 30_000) -> Any:
    world.script(protocol_reply(wait_ms))
    return world.advance(world.request())


def _slow_receive(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[threading.Event, threading.Event]:
    """Runner A (a non-main thread) has its reply in hand but records it late."""
    mod = sitfis()
    real = mod._receive
    in_hand = threading.Event()
    go = threading.Event()

    def slow(tenant_id: Any, report_id: Any, call_id: Any, reply: Any) -> None:
        if (
            threading.current_thread() is not threading.main_thread()
            and not go.is_set()
        ):
            in_hand.set()
            go.wait(timeout=30)
        real(tenant_id, report_id, call_id, reply)

    monkeypatch.setattr(mod, "_receive", slow)
    return in_hand, go


# --- R1: a stale runner's reply overwrites the newer call's recorded wait ---------


def test_r1a_stale_reply_overwrites_a_newer_202_wait_after_a_death(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _protocol(world)
    world.clock.advance(seconds=30)
    world.script(processing_202(5_000))  # attempt 1's reply, which A records late
    in_hand, go = _slow_receive(monkeypatch)
    mod = sitfis()
    outcome: dict[str, Any] = {}

    def runner_a() -> None:
        try:
            outcome["a"] = world.advance(report).state
        except BaseException as error:  # noqa: BLE001
            outcome["a"] = f"raised {type(error).__name__}"
        finally:
            connections.close_all()

    a = threading.Thread(target=runner_a)
    a.start()
    try:
        assert in_hand.wait(timeout=15)
        world.clock.advance(seconds=121)  # A's lease ran out while it held the reply
        outcome["b"] = world.advance(report).state  # B quarantines attempt 1
        snap(world, report, "r1a-after-b")
        world.clock.advance(seconds=60)
        world.script(processing_202(300_000))  # SERPRO: come back in 300 s
        real_perform = mod.perform

        def die_after_final(*args: object, **kwargs: object) -> object:
            real_perform(*args, **kwargs)
            raise SystemExit("death after the final 202")

        monkeypatch.setattr(mod, "perform", die_after_final)
        with pytest.raises(SystemExit):
            world.advance(report)  # C: attempt 2, final 202 of 300 s, then dies
        monkeypatch.setattr(mod, "perform", real_perform)
        snap(world, report, "r1a-after-c-death")
    finally:
        go.set()
        a.join(timeout=30)
    assert not a.is_alive()
    after_a = snap(world, report, "r1a-after-stale-a")
    world.advance(report)  # D: the next delivery
    after_d = snap(world, report, "r1a-after-d")
    world.clock.advance(seconds=60)
    world.script(processing_202(240_000))
    world.advance(report)
    at_60 = snap(world, report, "r1a-at-plus-60s")
    print("RV2_R1A", json.dumps(outcome), flush=True)
    # The repaired claim: call 2's 300 s wait is honoured by whichever runner applies it.
    assert after_d["wait_until_minus_now_s"] == 300.0, (after_a, after_d)
    assert at_60["emitir_requests"] == 2


def test_r1b_stale_reply_overwrites_a_newer_202_wait_without_a_death(
    world: World, monkeypatch: pytest.MonkeyPatch
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
        except BaseException as error:  # noqa: BLE001
            outcome["a"] = f"raised {type(error).__name__}"
        finally:
            connections.close_all()

    a = threading.Thread(target=runner_a)
    a.start()
    try:
        assert in_hand.wait(timeout=15)
        world.clock.advance(seconds=121)
        outcome["b"] = world.advance(report).state
        world.clock.advance(seconds=60)
        world.script(processing_202(300_000))
        real_perform = mod.perform

        def hold_after_final(*args: object, **kwargs: object) -> object:
            # C is alive: right after its 202 is final, the stale A records its reply
            # and runs its own next pass before C runs its own.
            result = real_perform(*args, **kwargs)
            if threading.current_thread() is threading.main_thread():
                go.set()
                a.join(timeout=30)
            return result

        monkeypatch.setattr(mod, "perform", hold_after_final)
        outcome["c"] = world.advance(report).state
        monkeypatch.setattr(mod, "perform", real_perform)
    finally:
        go.set()
        a.join(timeout=30)
    assert not a.is_alive()
    after = snap(world, report, "r1b-after-a-and-c")
    world.clock.advance(seconds=60)
    world.script(processing_202(240_000))
    world.advance(report)
    at_60 = snap(world, report, "r1b-at-plus-60s")
    print("RV2_R1B", json.dumps(outcome), flush=True)
    assert after["wait_until_minus_now_s"] == 300.0, after
    assert at_60["emitir_requests"] == 2


# --- R2: the one-day cap ------------------------------------------------------------


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("202-3-days", 86_400.0),
        ("204-etag-huge", 86_400.0),
        ("202-negative", 60.0),
        ("202-exactly-one-day", 86_400.0),
    ],
)
def test_r2_waits_above_one_day_are_capped_at_one_day(
    world: World, reply: str, expected: float
) -> None:
    report = _protocol(world)
    world.clock.advance(seconds=30)
    replies = {
        "202-3-days": processing_202(3 * 86_400_000),
        "204-etag-huge": Reply(
            status=204, body=b"", headers=(("ETag", '"tempoEspera:9999999999"'),)
        ),
        "202-negative": processing_202(-5),
        "202-exactly-one-day": processing_202(86_400_000),
    }
    world.script(replies[reply])
    world.advance(report)
    s = snap(world, report, f"r2-{reply}")
    assert s["wait_until_minus_now_s"] == expected


def test_r2b_a_protocol_wait_above_one_day_is_capped(world: World) -> None:
    report = _protocol(world, wait_ms=2 * 86_400_000)
    s = snap(world, report, "r2b-protocol-2-days")
    assert s["wait_until_minus_now_s"] == 86_400.0


# --- R3: a DB error while storing a paid PDF ---------------------------------------


def test_r4_a_continuation_racing_a_live_runner_sends_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = module("apps.integrations.serpro.sitfis_tasks")
    scheduled: list[Any] = []

    def capture(args: Any, kwargs: Any = None, **options: Any) -> None:
        scheduled.append(options.get("eta"))

    monkeypatch.setattr(tasks.advance_situation_report_task, "apply_async", capture)
    report = _protocol(world)
    world.clock.advance(seconds=30)
    world.script(Reply(body=report_reply().body, held=True))
    world.gateway.received.clear()
    outcome: dict[str, Any] = {}

    def first() -> None:
        try:
            outcome["a"] = world.advance(report).state
        finally:
            connections.close_all()

    runner = threading.Thread(target=first)
    runner.start()
    try:
        assert world.gateway.received.wait(timeout=10)
        scheduled.clear()
        outcome["retry"] = tasks.advance_situation_report_task(
            str(world.tenant_id), str(report.pk), failures=1
        )
        outcome["retry_eta_s"] = [
            (eta - world.clock.now()).total_seconds() for eta in scheduled
        ]
        snap(world, report, "r4-during-live-lease")
    finally:
        world.gateway.release.set()
        runner.join(timeout=15)
    assert not runner.is_alive()
    final = snap(world, report, "r4-final")
    print("RV2_R4", json.dumps(outcome, default=str), flush=True)
    assert final["state"] == "emitido"
    assert final["emitir_requests"] == 1
    assert outcome["retry"] == "emitindo"


# --- R5: a stall on every attempt (review 1's P1b harness, persistent) -------------


def test_r5_persistent_stall_before_dispatch_start(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _protocol(world)
    world.clock.advance(seconds=30)
    mod = sitfis()
    real = mod.perform
    start = world.clock.now()

    def stall_then_perform(*args: object, **kwargs: object) -> object:
        world.clock.advance(seconds=121)
        return real(*args, **kwargs)

    monkeypatch.setattr(mod, "perform", stall_then_perform)
    first = world.advance(report)
    monkeypatch.setattr(mod, "perform", real)
    s = snap(world, first, "r5-after-one-delivery")
    print("RV2_R5_ELAPSED_S", (world.clock.now() - start).total_seconds(), flush=True)
    assert s["state"] == "limite_de_tentativas"
    assert s["emitir_requests"] == 0
    assert s["report_problems"] == ["aberta"]


# --- R6: a hard kill is redelivered by the broker, not by in-process code ----------


def test_r6_task_is_redelivered_after_a_worker_loss() -> None:
    tasks = module("apps.integrations.serpro.sitfis_tasks")
    task = tasks.advance_situation_report_task
    seen = {
        "CELERY_TASK_ACKS_LATE": django_settings.CELERY_TASK_ACKS_LATE,
        "CELERY_TASK_REJECT_ON_WORKER_LOST": django_settings.CELERY_TASK_REJECT_ON_WORKER_LOST,
        "task.acks_late": task.acks_late,
        "task.reject_on_worker_lost": task.reject_on_worker_lost,
        "task.autoretry_for": getattr(task, "autoretry_for", None),
        "CELERY_TASK_TIME_LIMIT": django_settings.CELERY_TASK_TIME_LIMIT,
        "CELERY_TASK_SOFT_TIME_LIMIT": django_settings.CELERY_TASK_SOFT_TIME_LIMIT,
        "broker_transport_options": getattr(
            django_settings, "CELERY_BROKER_TRANSPORT_OPTIONS", None
        ),
    }
    print("RV2_R6", json.dumps(seen, default=str), flush=True)
    assert task.acks_late is True
    assert task.reject_on_worker_lost is True


# --- R7: a fresh report after the limit is billed once -----------------------------


def test_r7_new_report_after_three_unsent_is_billed_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _protocol(world)
    world.clock.advance(seconds=30)
    mod = sitfis()
    real = mod.perform

    def die(*args: object, **kwargs: object) -> object:
        raise SystemExit("death after the claim commit")

    for _ in range(3):
        monkeypatch.setattr(mod, "perform", die)
        with pytest.raises(SystemExit):
            world.advance(report)
        monkeypatch.setattr(mod, "perform", real)
        world.clock.advance(seconds=121)
    limit = world.advance(report)
    snap(world, limit, "r7-limit")
    world.script(protocol_reply(30_000))
    fresh = world.advance(world.request())
    world.clock.advance(seconds=30)
    world.script(report_reply())
    done = world.advance(fresh)
    s = snap(world, done, "r7-fresh-emitted")
    assert limit.state == "limite_de_tentativas"
    assert done.state == "emitido" and done.pk != report.pk
    assert s["emitir_requests"] == 1
    assert s["known_liability"] == 320_000
    assert s["reserved"] == 0


# --- R8: death between recording a 202 and its finalization -------------------------


def test_r8_death_between_recording_and_finalizing_a_202(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.integrations import call_protocol  # noqa: PLC0415

    report = _protocol(world)
    world.clock.advance(seconds=30)
    world.script(processing_202(300_000))
    real_finalize = call_protocol.finalize

    def broken(*_a: object, **_k: object) -> bool:
        raise SystemExit("worker died inside the finalize transaction")

    monkeypatch.setattr(call_protocol, "finalize", broken)
    with pytest.raises(SystemExit):
        world.advance(report)
    monkeypatch.setattr(call_protocol, "finalize", real_finalize)
    snap(world, report, "r8-after-death")
    world.clock.advance(seconds=121)
    world.advance(report)
    s = snap(world, report, "r8-after-lease")
    assert s["state"] == "aguardando"
    assert s["wait_until_minus_now_s"] == 300.0
    assert s["emitir_requests"] == 1


# --- R9: a 204 received after an unsent identity ----------------------------------


def test_r9_unsent_then_204_keeps_the_etag_wait(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _protocol(world)
    world.clock.advance(seconds=30)
    mod = sitfis()
    real = mod.perform

    def die(*args: object, **kwargs: object) -> object:
        raise SystemExit("death after the claim commit")

    monkeypatch.setattr(mod, "perform", die)
    with pytest.raises(SystemExit):
        world.advance(report)
    monkeypatch.setattr(mod, "perform", real)
    world.clock.advance(seconds=121)
    world.script(processing_204(180_000))
    world.advance(report)
    s = snap(world, report, "r9-after-unsent-then-204")
    assert s["emission_attempts"] == 2
    assert s["wait_until_minus_now_s"] == 180.0
    assert s["emitir_requests"] == 1
    assert s["known_liability"] == 0
