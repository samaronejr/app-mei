"""SITFIS recovery: every emission outcome is owned, nothing paid is lost or re-bought.

AXRM-016 repair 1 (review 1 of `bebff5a`): RV1-UNSENT-EMISSION-LIVELOCK,
RV1-202-WAIT-REPLACED-BY-DEFAULT and RV1-RAISE-STRANDS-PAID-REPORT, plus the
same-class paid-PDF re-buy and the coherent PDF size limit. Review 1's probes
P1a-c, P2a-b, P3a2, P3b2 and P4 are adapted here with the oracle the plan asks
for: the same harness, the repaired expectation.
"""

import base64
import threading
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import OperationalError, connections

from apps.core.tenancy import tenant_context
from apps.cycles.models import Exception as CycleException
from apps.cycles.models import ServiceCycle
from apps.cycles.scanning import EICAR, FakeScanner
from apps.integrations.call_models import ProviderCall
from apps.obligations.models import Document, FiscalSituationReport
from tests.fakes.provider import Reply
from tests.integrations.sitfis_support import (
    EMISSAO_MICROBRL,
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

REPORT_FAILED = "situacao_fiscal_nao_obtida"
FINAL = {
    "emitido",
    "negado",
    "falha",
    "incerto",
    "protocolo_expirado",
    "limite_de_tentativas",
}


def _protocol(world: World, wait_ms: int = 30_000) -> FiscalSituationReport:
    world.script(protocol_reply(wait_ms))
    return world.advance(world.request())


def _problems(world: World) -> list[str]:
    with tenant_context(world.tenant_id):
        return list(
            CycleException.objects.filter(reason_code=REPORT_FAILED).values_list(
                "state", flat=True
            )
        )


def _documents(world: World) -> int:
    with tenant_context(world.tenant_id):
        return Document.objects.filter(acquisition_origin="provedor_federal").count()


def _calls(world: World) -> list[tuple[Any, ...]]:
    with tenant_context(world.tenant_id):
        return [
            (c.state, c.attempts, c.http_status, c.billed)
            for c in ProviderCall.objects.filter(operation="SP-SIT-R").order_by(
                "created_at"
            )
        ]


def _wait_left(world: World, report: FiscalSituationReport) -> float | None:
    current = world.reload(report)
    if current.wait_until is None:
        return None
    return (current.wait_until - world.clock.now()).total_seconds()


def _capture(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[dict[str, Any]]]:
    tasks = module("apps.integrations.serpro.sitfis_tasks")
    scheduled: list[dict[str, Any]] = []

    def capture(
        args: tuple[Any, ...],
        kwargs: dict[str, Any] | None = None,
        **options: Any,  # noqa: ANN401
    ) -> None:
        scheduled.append({"args": tuple(args), "kwargs": kwargs or {}, **options})

    monkeypatch.setattr(tasks.advance_situation_report_task, "apply_async", capture)
    return tasks, scheduled


def _die_after_claim(
    monkeypatch: pytest.MonkeyPatch, world: World, report: FiscalSituationReport
) -> None:
    """Review 1's P1a death: the claim committed, the dispatch start never did."""
    mod = sitfis()
    real = mod.perform

    def die(*args: object, **kwargs: object) -> object:
        message = "simulated worker death after the claim commit"
        raise SystemExit(message)

    monkeypatch.setattr(mod, "perform", die)
    with pytest.raises(SystemExit):
        world.advance(report)
    monkeypatch.setattr(mod, "perform", real)


# --- B1 RV1-UNSENT-EMISSION-LIVELOCK -----------------------------------------------


def test_p1a_an_unsent_emission_is_counted_and_the_next_attempt_is_sent(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a claimed emission whose runner died before the dispatch start.
    report = _protocol(world)
    world.clock.advance(seconds=30)
    _die_after_claim(monkeypatch, world, report)
    world.clock.advance(seconds=121)
    world.script(report_reply())
    # When deliveries keep arriving, then the report never loops: the unsent
    # identity is counted and the next one is sent at once (nothing was paid).
    seen = []
    for _ in range(12):
        current = world.advance(report)
        seen.append((current.state, current.emission_attempts))
        if current.state in FINAL:
            break
        world.clock.advance(seconds=61)
    assert seen[-1] == ("emitido", 2)
    assert [c[:2] for c in _calls(world)] == [("incerta", 0), ("sucesso", 1)]
    assert len(world.sent("/Emitir")) == 1
    assert _problems(world) == []


def test_p1b_a_live_runner_stalled_before_dispatch_start_does_not_livelock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _protocol(world)
    world.clock.advance(seconds=30)
    mod = sitfis()
    real = mod.perform

    stalled: list[bool] = []

    def stall_then_perform(*args: object, **kwargs: object) -> object:
        # One stall only (review 1's P1b): the lease runs out before this
        # attempt's dispatch start; a later attempt is not stalled.
        if not stalled:
            stalled.append(True)
            world.clock.advance(seconds=121)
        return real(*args, **kwargs)

    world.script(report_reply())
    monkeypatch.setattr(mod, "perform", stall_then_perform)
    first = world.advance(report)
    monkeypatch.setattr(mod, "perform", real)
    states = [(first.state, first.emission_attempts)]
    for _ in range(8):
        if states[-1][0] in FINAL:
            break
        world.clock.advance(seconds=61)
        current = world.advance(report)
        states.append((current.state, current.emission_attempts))
    assert states[-1][0] == "emitido"
    assert states[-1][1] <= 3
    assert len(world.sent("/Emitir")) == 1


def test_p1c_the_task_body_stops_rescheduling_once_the_report_is_final(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks, scheduled = _capture(monkeypatch)
    report = _protocol(world)
    world.clock.advance(seconds=30)
    _die_after_claim(monkeypatch, world, report)
    world.clock.advance(seconds=121)
    world.script(report_reply())
    results = []
    for _ in range(6):
        scheduled.clear()
        state = tasks.advance_situation_report_task(
            str(world.tenant_id), str(report.pk)
        )
        results.append((state, len(scheduled)))
        if state in FINAL:
            break
        world.clock.advance(seconds=61)
    assert results[-1] == ("emitido", 0)
    assert len(results) < 6


def test_three_unsent_emissions_reach_the_limit_with_owned_work(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every emission identity counts toward the cap, sent or not: three claims
    # that never reached the wire end the report owned, never in an endless wait.
    report = _protocol(world)
    world.clock.advance(seconds=30)
    for _ in range(3):
        _die_after_claim(monkeypatch, world, report)
        world.clock.advance(seconds=121)
    final = world.advance(report)
    assert (final.state, final.emission_attempts) == ("limite_de_tentativas", 3)
    assert world.sent("/Emitir") == []
    assert _problems(world) == ["aberta"]
    assert world.budget().reserved == 0
    # A new request is no longer handed the stuck report.
    world.script(protocol_reply())
    assert world.request().pk != report.pk


# --- B2 RV1-202-WAIT-REPLACED-BY-DEFAULT -------------------------------------------


def test_p2a_a_dead_runners_202_wait_is_honoured_by_the_next_runner(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _protocol(world)
    world.clock.advance(seconds=30)
    world.script(processing_202(300_000))  # SERPRO: come back in 300 s
    mod = sitfis()
    real = mod.perform

    def die_after_final(*args: object, **kwargs: object) -> object:
        real(*args, **kwargs)
        message = "simulated worker death after the final 202"
        raise SystemExit(message)

    monkeypatch.setattr(mod, "perform", die_after_final)
    with pytest.raises(SystemExit):
        world.advance(report)
    monkeypatch.setattr(mod, "perform", real)
    recovered = world.advance(report)
    assert recovered.state == "aguardando"
    assert _wait_left(world, report) == 300.0
    # No billed poll before SERPRO's estimate.
    world.clock.advance(seconds=60)
    world.script(processing_202(240_000))
    world.advance(report)
    assert len(world.sent("/Emitir")) == 1
    world.clock.advance(seconds=240)
    world.advance(report)
    assert len(world.sent("/Emitir")) == 2


def test_p2b_a_duplicate_runner_applies_the_202_with_the_providers_wait(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _protocol(world)
    world.clock.advance(seconds=30)
    world.script(processing_202(300_000))
    mod = sitfis()
    real = mod.perform
    finalized = threading.Event()
    go = threading.Event()
    outcome: dict[str, Any] = {}

    def perform_then_hold(*args: object, **kwargs: object) -> object:
        result = real(*args, **kwargs)
        if threading.current_thread() is not threading.main_thread():
            finalized.set()
            go.wait(timeout=15)
        return result

    monkeypatch.setattr(mod, "perform", perform_then_hold)

    def first() -> None:
        try:
            outcome["a"] = world.advance(report).state
        finally:
            connections.close_all()

    runner = threading.Thread(target=first)
    runner.start()
    try:
        assert finalized.wait(timeout=15)
        outcome["b"] = world.advance(report).state
    finally:
        go.set()
        runner.join(timeout=15)
    assert not runner.is_alive()
    assert outcome == {"a": "aguardando", "b": "aguardando"}
    assert _wait_left(world, report) == 300.0
    assert len(world.sent("/Emitir")) == 1


def test_a_dead_runners_204_etag_wait_is_honoured(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _protocol(world)
    world.clock.advance(seconds=30)
    world.script(processing_204(180_000))
    mod = sitfis()
    real = mod.perform

    def die_after_final(*args: object, **kwargs: object) -> object:
        real(*args, **kwargs)
        message = "simulated worker death after the final 204"
        raise SystemExit(message)

    monkeypatch.setattr(mod, "perform", die_after_final)
    with pytest.raises(SystemExit):
        world.advance(report)
    monkeypatch.setattr(mod, "perform", real)
    world.advance(report)
    assert _wait_left(world, report) == 180.0


def test_an_estimate_above_one_hour_is_honoured_not_replaced_by_the_default(
    world: World,
) -> None:
    report = _protocol(world)
    world.clock.advance(seconds=30)
    world.script(processing_202(2 * 3_600_000))
    world.advance(report)
    assert _wait_left(world, report) == 7200.0


# --- B3 RV1-RAISE-STRANDS-PAID-REPORT ----------------------------------------------


def test_p3a2_a_storage_failure_on_a_paid_pdf_is_billed_and_owned(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks, scheduled = _capture(monkeypatch)
    report = _protocol(world)
    scheduled.clear()
    world.clock.advance(seconds=30)
    world.script(report_reply())
    mod = sitfis()

    def storage_down(**kwargs: object) -> object:
        message = "object storage unavailable"
        raise OSError(message)

    monkeypatch.setattr(mod, "ingest_system_document", storage_down)
    # The delivery does not raise: the paid reply is recorded and owned.
    state = tasks.advance_situation_report_task(str(world.tenant_id), str(report.pk))
    assert state == "falha"
    assert _calls(world) == [("falhou", 1, 200, "sim")]
    assert world.budget().known_liability == EMISSAO_MICROBRL
    assert world.budget().reserved == 0
    assert _problems(world) == ["aberta"]
    assert scheduled == []
    # Never re-bought automatically.
    world.clock.advance(days=3)
    world.advance(report)
    assert len(world.sent("/Emitir")) == 1


def test_p3b2_a_db_error_after_a_final_202_schedules_a_bounded_continuation(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks, scheduled = _capture(monkeypatch)
    report = _protocol(world)
    scheduled.clear()
    world.clock.advance(seconds=30)
    world.script(processing_202(30_000))
    mod = sitfis()
    real_apply = mod._apply

    def connection_lost(*args: object, **kwargs: object) -> None:
        message = "server closed the connection unexpectedly"
        raise OperationalError(message)

    monkeypatch.setattr(mod, "_apply", connection_lost)
    with pytest.raises(OperationalError):
        tasks.advance_situation_report_task(str(world.tenant_id), str(report.pk))
    monkeypatch.setattr(mod, "_apply", real_apply)
    # The failed delivery queued its own continuation (a bounded retry).
    [retry] = scheduled
    assert retry["args"] == (str(world.tenant_id), str(report.pk))
    assert retry["kwargs"] == {"failures": 1}
    assert retry["eta"] > world.clock.now()
    # The continuation applies the billed 202 with SERPRO's 30 s wait.
    scheduled.clear()
    state = tasks.advance_situation_report_task(
        str(world.tenant_id), str(report.pk), **retry["kwargs"]
    )
    assert state == "aguardando"
    assert _wait_left(world, report) == 30.0
    assert _calls(world) == [("aguardando_resultado", 1, 202, "sim")]
    assert len(world.sent("/Emitir")) == 1


def test_repeated_failures_end_in_owned_work_and_stop_retrying(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks, scheduled = _capture(monkeypatch)
    report = _protocol(world)
    world.clock.advance(seconds=30)
    world.script(processing_202(30_000))
    mod = sitfis()
    real_apply = mod._apply

    def connection_lost(*args: object, **kwargs: object) -> None:
        message = "server closed the connection unexpectedly"
        raise OperationalError(message)

    monkeypatch.setattr(mod, "_apply", connection_lost)
    failures = 0
    for _ in range(10):
        scheduled.clear()
        with pytest.raises(OperationalError):
            tasks.advance_situation_report_task(
                str(world.tenant_id), str(report.pk), failures=failures
            )
        if not scheduled:
            break
        failures = scheduled[0]["kwargs"]["failures"]
    monkeypatch.setattr(mod, "_apply", real_apply)
    assert scheduled == []
    assert 0 < failures < 10
    assert _problems(world) == ["aberta"]
    assert world.reload(report).state == "emitindo"


# --- Same class: a paid PDF that was received is never bought again --------------


def test_p4_a_paid_pdf_received_after_lease_expiry_is_adopted_not_rebought(
    world: World,
) -> None:
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
        world.clock.advance(seconds=121)
        outcome["b"] = world.advance(report).state
    finally:
        world.gateway.release.set()
        runner.join(timeout=15)
    assert not runner.is_alive()
    world.clock.advance(seconds=60)
    world.script(report_reply())
    final = world.advance(report)
    assert final.state == "emitido"
    assert final.document_id is not None
    assert _documents(world) == 1
    assert len(world.sent("/Emitir")) == 1


def test_a_paid_pdf_whose_finalization_raised_is_adopted_not_rebought(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.integrations import call_protocol  # noqa: PLC0415

    report = _protocol(world)
    world.clock.advance(seconds=30)
    world.script(report_reply())
    mod = sitfis()
    real_perform = mod.perform

    real_finalize = call_protocol.finalize

    def broken(*_a: object, **_k: object) -> bool:
        message = "worker died inside the finalize transaction"
        raise SystemExit(message)

    monkeypatch.setattr(call_protocol, "finalize", broken)
    with pytest.raises(SystemExit):
        world.advance(report)
    monkeypatch.setattr(call_protocol, "finalize", real_finalize)
    del real_perform
    world.clock.advance(seconds=121)
    world.script(report_reply())
    final = world.advance(report)
    assert final.state == "emitido"
    assert _documents(world) == 1
    assert len(world.sent("/Emitir")) == 1


def test_a_received_protocol_whose_finalization_died_is_adopted(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.integrations import call_protocol  # noqa: PLC0415

    real_finalize = call_protocol.finalize

    def broken(*_a: object, **_k: object) -> bool:
        message = "worker died inside the finalize transaction"
        raise SystemExit(message)

    world.script(protocol_reply(30_000))
    report = world.request()
    monkeypatch.setattr(call_protocol, "finalize", broken)
    with pytest.raises(SystemExit):
        world.advance(report)
    monkeypatch.setattr(call_protocol, "finalize", real_finalize)
    adopted = world.advance(report)
    assert adopted.state == "aguardando"
    assert _wait_left(world, report) == 30.0
    assert len(world.sent("/Apoiar")) == 1
    world.clock.advance(seconds=30)
    world.script(report_reply())
    assert world.advance(report).state == "emitido"
    assert len(world.sent("/Apoiar")) == 1


# --- Same class: one coherent PDF size limit ----------------------------------------


def test_the_pdf_limit_is_the_largest_report_the_wire_can_carry() -> None:
    mod = sitfis()
    limit = getattr(mod, "MAX_REPORT_BYTES", None)
    assert limit is not None, "AXRM-016 repair must define one SITFIS PDF limit"
    from apps.integrations.transport_wire import XML_LIMIT  # noqa: PLC0415

    # base64 grows bytes by 4/3; the escaped JSON envelope must still fit.
    assert len(base64.b64encode(b"x" * limit)) < XML_LIMIT
    assert limit > 3 * 1024 * 1024


def test_a_report_at_the_limit_is_stored_and_one_above_fails_owned(
    world: World,
) -> None:
    mod = sitfis()
    limit = getattr(mod, "MAX_REPORT_BYTES", 0)
    assert limit, "AXRM-016 repair must define one SITFIS PDF limit"
    head, tail = b"%PDF-1.7\n", b"\n%%EOF\n"
    report = _protocol(world)
    world.clock.advance(seconds=30)
    world.script(report_reply(head + b"0" * (limit - len(head) - len(tail)) + tail))
    assert world.advance(report).state == "emitido"
    world.script(protocol_reply(0), report_reply(head + b"0" * (limit + 4096) + tail))
    failed = world.advance(world.request())
    assert failed.state == "falha"
    assert len(world.sent("/Emitir")) == 2


# --- Review refusal for an infected report (review 1 N4) ---------------------------


def test_an_infected_report_cannot_be_reviewed(world: World) -> None:
    from unittest.mock import patch  # noqa: PLC0415

    from apps.cycles.tasks import scan_quarantine  # noqa: PLC0415

    reviews = module("apps.cycles.services.federal_review")
    report = _protocol(world)
    world.clock.advance(seconds=30)
    world.script(report_reply(b"%PDF-1.7\n" + EICAR + b"\n%%EOF\n"))
    emitted = world.advance(report)
    with patch("apps.cycles.tasks.scanner_from_settings", return_value=FakeScanner()):
        scan_quarantine.apply().get()
    with tenant_context(world.tenant_id):
        current = FiscalSituationReport.objects.get(pk=emitted.pk)
        version = ServiceCycle.objects.get(pk=world.cycle.pk).version
        with pytest.raises(ValidationError):
            reviews.record_federal_report_review(
                world.responsible,
                world.cycle.pk,
                current.pk,
                outcome="sem_pendencias_nas_fontes",
                pendencias=[],
                expected_version=version,
                input_digest=reviews.report_input_digest(current),
            )
