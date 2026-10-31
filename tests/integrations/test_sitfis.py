"""SITFIS: free protocol, paid emission only at/after `tempoEspera`, owned failures.

AXRM-016 acceptance (plan todo 16; A6 SP-SIT-P/SP-SIT-R, A7.3). Every call runs
through the real AXRM-009 protocol, AXRM-010 budget and AXRM-013 adapter against
the scripted loopback SERPRO; the clock is injected, nothing sleeps.
"""

import json
import logging
import threading
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connections
from django.http import Http404
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake, IntakeState
from apps.cycles.models import CycleEvent, ServiceCycle
from apps.cycles.models import Exception as CycleException
from apps.integrations.authority import head_record, revoke_authority
from apps.integrations.call_contracts import Claim
from apps.integrations.call_models import CallState
from apps.integrations.models import IntegrationIssue
from apps.obligations.models import (
    Document,
    FederalConsultation,
    FiscalSituationReport,
)
from apps.tenants.models import Membership, TenantRole
from tests.fakes.provider import Reply
from tests.integrations.sitfis_support import (
    EMISSAO_MICROBRL,
    PDF,
    PROTOCOL,
    Clock,
    World,
    clock,
    denied_403,
    envelope,
    processing_202,
    processing_204,
    protocol_reply,
    report_reply,
    sitfis,
    world,
)

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "world"]

REPORT_FAILED = "situacao_fiscal_nao_obtida"


def _protocol_received(
    world: World, wait_ms: int | None = 30_000
) -> FiscalSituationReport:
    world.script(protocol_reply(wait_ms))
    report = world.request()
    return world.advance(report)


def _exceptions(world: World, reason: str) -> list[CycleException]:
    with tenant_context(world.tenant_id):
        return list(CycleException.objects.filter(reason_code=reason))


def _emission_bodies(world: World) -> list[dict[str, Any]]:
    return [json.loads(request.body) for request in world.sent("/Emitir")]


def test_protocol_is_free_and_nothing_is_emitted_before_wait_until(
    world: World,
) -> None:
    # Given a fake protocol answering tempoEspera 30 000 ms (30 s).
    report = _protocol_received(world)
    # Then the free /Apoiar request carries empty data and the protocol is stored.
    [apoiar] = world.sent("/Apoiar")
    pedido = json.loads(apoiar.body)["pedidoDados"]
    assert (pedido["idSistema"], pedido["idServico"]) == (
        "SITFIS",
        "SOLICITARPROTOCOLO91",
    )
    assert json.loads(pedido["dados"]) in ("", {})
    assert report.state == "aguardando"
    assert report.protocol == PROTOCOL
    assert report.wait_until == world.clock.now() + timedelta(seconds=30)
    assert report.emission_attempts == 0
    [protocol_call] = world.calls("SP-SIT-P")
    assert (protocol_call.state, protocol_call.billed) == ("sucesso", "nao")
    assert world.budget().known_liability == 0
    # When a runner arrives one second early, then nothing is sent or reserved.
    world.clock.advance(seconds=29)
    assert world.advance(report).state == "aguardando"
    assert world.sent("/Emitir") == []
    assert world.calls("SP-SIT-R") == []
    assert world.budget().reserved == 0
    # When the wait has passed, then exactly one emission stores the PDF privately.
    world.clock.advance(seconds=1)
    world.script(report_reply())
    emitted = world.advance(report)
    [body] = _emission_bodies(world)
    assert json.loads(body["pedidoDados"]["dados"]) == {"protocoloRelatorio": PROTOCOL}
    assert emitted.state == "emitido"
    assert emitted.emission_attempts == 1
    assert emitted.emitted_at == world.clock.now()
    with tenant_context(world.tenant_id):
        document = Document.objects.get(pk=emitted.document_id)
        intake = DocumentIntake.objects.get(document=document)
        consultation = FederalConsultation.objects.get(pk=emitted.consultation_id)
    [call] = world.calls("SP-SIT-R")
    assert (call.state, call.billed) == ("sucesso", "sim")
    assert (
        document.portal_visible,
        document.acquisition_origin,
        document.uploaded_by_id,
        document.acquisition_receipt_ref,
    ) == (False, "provedor_federal", None, str(call.pk))
    assert intake.state == IntakeState.QUARANTINED
    assert (consultation.kind, consultation.outcome, consultation.provider_ref) == (
        "situacao_fiscal",
        "encontrado",
        str(call.pk),
    )
    assert world.budget().known_liability == EMISSAO_MICROBRL


def test_absent_wait_defaults_to_sixty_seconds_and_is_logged(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="apps.integrations.serpro.sitfis")
    report = _protocol_received(world, wait_ms=None)
    assert report.wait_until == world.clock.now() + timedelta(seconds=60)
    assert [
        record
        for record in caplog.records
        if getattr(record, "sitfis_event", "") == "espera_padrao"
    ]


def test_202_is_billed_and_reschedules_with_its_new_wait(world: World) -> None:
    report = _protocol_received(world)
    world.clock.advance(seconds=30)
    world.script(processing_202(4_000))
    waiting = world.advance(report)
    [call] = world.calls("SP-SIT-R")
    assert (call.http_status, call.billed) == (202, "sim")
    assert waiting.state == "aguardando"
    assert waiting.emission_attempts == 1
    assert waiting.wait_until == world.clock.now() + timedelta(seconds=4)
    assert world.budget().known_liability == EMISSAO_MICROBRL
    # Not before the new wait ends.
    world.clock.advance(seconds=3)
    world.advance(report)
    assert len(world.sent("/Emitir")) == 1
    world.clock.advance(seconds=1)
    world.script(report_reply())
    assert world.advance(report).state == "emitido"
    assert [c.billed for c in world.calls("SP-SIT-R")] == ["sim", "sim"]
    assert world.budget().known_liability == 2 * EMISSAO_MICROBRL


def test_204_is_not_billed_and_reschedules_with_the_etag_estimate(
    world: World,
) -> None:
    report = _protocol_received(world)
    world.clock.advance(seconds=30)
    world.script(processing_204(5_000))
    waiting = world.advance(report)
    [call] = world.calls("SP-SIT-R")
    assert (call.http_status, call.billed) == (204, "nao")
    assert (waiting.state, waiting.emission_attempts) == ("aguardando", 1)
    assert waiting.wait_until == world.clock.now() + timedelta(seconds=5)
    assert world.budget().known_liability == 0
    assert world.budget().reserved == 0


def test_two_202_then_500_reach_the_limit_with_owned_work_and_no_fourth_call(
    world: World,
) -> None:
    # Given 202, 202 and then a 500 from the emission endpoint.
    report = _protocol_received(world)
    for reply in (processing_202(1_000), processing_202(1_000)):
        world.clock.advance(seconds=30)
        world.script(reply)
        world.advance(report)
    world.clock.advance(seconds=30)
    world.script(Reply(status=500, body=envelope(500, "")))
    limited = world.advance(report)
    # Then two billed attempts, one unbilled 500, the limit state and owned work.
    calls = world.calls("SP-SIT-R")
    assert [(c.http_status, c.billed) for c in calls] == [
        (202, "sim"),
        (202, "sim"),
        (500, "nao"),
    ]
    assert (limited.state, limited.emission_attempts) == ("limite_de_tentativas", 3)
    [problem] = _exceptions(world, REPORT_FAILED)
    assert (problem.subject_ref, problem.owner_id, problem.state) == (
        report.pk,
        world.responsible.pk,
        "aberta",
    )
    with tenant_context(world.tenant_id):
        consultation = FederalConsultation.objects.get(pk=limited.consultation_id)
    assert consultation.outcome == "falha"
    assert world.budget().known_liability == 2 * EMISSAO_MICROBRL
    # When a redelivered or scheduled runner arrives later, then no fourth call.
    world.clock.advance(hours=1)
    world.script(report_reply())
    assert world.advance(report).state == "limite_de_tentativas"
    assert len(world.sent("/Emitir")) == 3
    assert len(_exceptions(world, REPORT_FAILED)) == 1


def test_a_limit_without_an_open_cycle_is_owned_as_an_integration_issue(
    world: World,
) -> None:
    with tenant_context(world.tenant_id):
        ServiceCycle.objects.filter(pk=world.cycle.pk).update(state="cancelado")
    report = _protocol_received(world)
    for _ in range(3):
        world.clock.advance(seconds=30)
        world.script(processing_202(1_000))
        world.advance(report)
    assert world.reload(report).state == "limite_de_tentativas"
    with tenant_context(world.tenant_id):
        issue = IntegrationIssue.objects.get(reason=REPORT_FAILED)
    assert (issue.client_id, issue.owner_id, issue.state) == (
        world.termo.client.pk,
        world.responsible.pk,
        "aberta",
    )
    assert issue.service == "SP-SIT-R"


@pytest.mark.parametrize(
    "dados",
    [
        json.dumps({"pdf": "not base64 !!"}),
        json.dumps({"pdf": "bm90IGEgcGRm"}),
        json.dumps({}),
        "not json",
    ],
)
def test_a_malformed_report_is_decided_before_finality_and_owned(
    world: World, dados: str
) -> None:
    report = _protocol_received(world)
    world.clock.advance(seconds=30)
    world.script(Reply(body=envelope(200, dados)))
    failed = world.advance(report)
    [call] = world.calls("SP-SIT-R")
    assert (call.state, call.billed) == ("falhou", "sim")
    assert failed.state == "falha"
    assert failed.document_id is None
    with tenant_context(world.tenant_id):
        assert not Document.objects.filter(
            acquisition_origin="provedor_federal"
        ).exists()
    assert len(_exceptions(world, REPORT_FAILED)) == 1
    # Never retried automatically.
    world.clock.advance(hours=1)
    world.advance(report)
    assert len(world.sent("/Emitir")) == 1


@pytest.mark.parametrize(
    "reply",
    [
        Reply(body=envelope(200, json.dumps({"tempoEspera": 1}))),
        Reply(body=envelope(200, "{not json")),
        Reply(body=b"<html>"),
        Reply(body=envelope(200, json.dumps({"protocoloRelatorio": ""}))),
    ],
)
def test_a_malformed_protocol_reply_fails_the_report_with_owned_work(
    world: World, reply: Reply
) -> None:
    world.script(reply)
    report = world.advance(world.request())
    assert report.state == "falha"
    assert report.protocol == ""
    assert len(_exceptions(world, REPORT_FAILED)) == 1
    assert world.sent("/Emitir") == []


def test_a_final_202_whose_runner_died_is_applied_by_the_next_runner(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Given a runner that dies right after the 202 is final, before applying it.
    report = _protocol_received(world)
    world.clock.advance(seconds=30)
    world.script(processing_202(4_000))
    module = sitfis()
    real: Callable[..., Any] = module.perform

    def die_after_final(*args: object, **kwargs: object) -> object:
        real(*args, **kwargs)
        message = "simulated worker death after the final reply"
        raise SystemExit(message)

    monkeypatch.setattr(module, "perform", die_after_final)
    with pytest.raises(SystemExit):
        world.advance(report)
    monkeypatch.setattr(module, "perform", real)
    assert [c.state for c in world.calls("SP-SIT-R")] == [CallState.WAITING]
    # When the next delivery runs, then the attempt counts with the default wait.
    caplog.set_level(logging.INFO, logger="apps.integrations.serpro.sitfis")
    recovered = world.advance(report)
    assert (recovered.state, recovered.emission_attempts) == ("aguardando", 1)
    assert recovered.wait_until == world.clock.now() + timedelta(seconds=60)
    assert len(world.sent("/Emitir")) == 1
    assert any(
        getattr(record, "sitfis_event", "") == "espera_padrao"
        for record in caplog.records
    )


def test_a_lease_lost_mid_emission_is_owned_and_counted(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a runner that dies after its dispatch-start commit, before any reply.
    report = _protocol_received(world)
    world.clock.advance(seconds=30)
    module = sitfis()
    real: Callable[..., Any] = module.perform

    def die_after_start(ticket: Claim, *args: object, **kwargs: object) -> object:
        from apps.integrations.call_protocol import begin_io  # noqa: PLC0415

        begin_io(ticket)
        message = "simulated worker death mid-emission"
        raise SystemExit(message)

    monkeypatch.setattr(module, "perform", die_after_start)
    with pytest.raises(SystemExit):
        world.advance(report)
    monkeypatch.setattr(module, "perform", real)
    # When another runner arrives while the lease is live, then nothing is sent.
    assert world.advance(report).emission_attempts == 0
    assert world.sent("/Emitir") == []
    # When the lease has run out, then the call is uncertain, owned and counted.
    world.clock.advance(seconds=121)
    counted = world.advance(report)
    [call] = world.calls("SP-SIT-R")
    assert call.state == CallState.UNCERTAIN
    with tenant_context(world.tenant_id):
        assert IntegrationIssue.objects.filter(
            reason="chamada_provedor_incerta", service="SP-SIT-R"
        ).exists()
    assert (counted.state, counted.emission_attempts) == ("aguardando", 1)
    assert counted.wait_until == world.clock.now() + timedelta(seconds=60)
    # And the next attempt may still obtain the report.
    world.clock.advance(seconds=60)
    world.script(report_reply())
    assert world.advance(report).state == "emitido"
    assert len(world.sent("/Emitir")) == 1


def test_concurrent_runners_send_one_emission(world: World) -> None:
    report = _protocol_received(world)
    world.clock.advance(seconds=30)
    world.script(Reply(body=report_reply().body, held=True))
    outcome: dict[str, Any] = {}

    def first() -> None:
        try:
            outcome["first"] = world.advance(report).state
        finally:
            connections.close_all()

    runner = threading.Thread(target=first)
    runner.start()
    assert world.gateway.received.wait(timeout=10)
    try:
        # The second runner sees the live in-flight emission and leaves it alone.
        second = world.advance(report)
        assert second.state == "emitindo"
        assert second.emission_attempts == 0
    finally:
        world.gateway.release.set()
        runner.join(timeout=15)
    assert not runner.is_alive()
    assert outcome["first"] == "emitido"
    assert len(world.sent("/Emitir")) == 1
    assert len(world.calls("SP-SIT-R")) == 1


def test_a_redelivered_runner_after_emission_sends_nothing(world: World) -> None:
    report = _protocol_received(world)
    world.clock.advance(seconds=30)
    world.script(report_reply())
    world.advance(report)
    world.clock.advance(hours=2)
    world.script(report_reply())
    again = world.advance(report)
    assert again.state == "emitido"
    assert len(world.sent("/Emitir")) == 1
    assert len(world.sent("/Apoiar")) == 1


def test_a_denied_emission_is_negado_with_owned_work_and_absent_authority(
    world: World,
) -> None:
    report = _protocol_received(world)
    world.clock.advance(seconds=30)
    world.script(denied_403())
    denied = world.advance(report)
    [call] = world.calls("SP-SIT-R")
    assert (call.state, call.billed) == (CallState.DENIED, "sim")
    assert denied.state == "negado"
    with tenant_context(world.tenant_id):
        head = head_record(world.termo.client.pk, "sitfis")
        consultation = FederalConsultation.objects.get(pk=denied.consultation_id)
    assert head is not None
    assert head.status == "ausente_detectada"
    assert consultation.outcome == "sem_procuracao"
    assert len(_exceptions(world, REPORT_FAILED)) == 1
    world.clock.advance(hours=1)
    world.advance(report)
    assert len(world.sent("/Emitir")) == 1


def test_a_pause_mid_flight_expires_the_protocol_without_a_call(
    world: World, settings: SettingsWrapper
) -> None:
    report = _protocol_received(world)
    settings.SERPRO_PAUSED = True
    world.clock.advance(seconds=30)
    expired = world.advance(report)
    assert expired.state == "protocolo_expirado"
    assert world.sent("/Emitir") == []
    assert world.budget().reserved == 0
    assert len(_exceptions(world, REPORT_FAILED)) == 1


def test_authority_revoked_at_dispatch_releases_the_reservation(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _protocol_received(world)
    world.clock.advance(seconds=30)
    module = sitfis()
    real: Callable[..., Any] = module.perform

    def revoke_then_perform(*args: object, **kwargs: object) -> object:
        with tenant_context(world.tenant_id):
            head = head_record(world.termo.client.pk, "sitfis")
            assert head is not None
            revoke_authority(
                world.termo.actor,
                world.termo.client.pk,
                service="sitfis",
                expected_version=head.version,
            )
        return real(*args, **kwargs)

    monkeypatch.setattr(module, "perform", revoke_then_perform)
    denied = world.advance(report)
    assert denied.state == "negado"
    assert world.sent("/Emitir") == []
    assert world.budget().reserved == 0
    assert world.budget().known_liability == 0
    assert len(_exceptions(world, REPORT_FAILED)) == 1


def test_a_request_without_the_flag_or_authority_writes_nothing(
    world: World, settings: SettingsWrapper
) -> None:
    module = sitfis()
    with tenant_context(world.tenant_id):
        head = head_record(world.termo.client.pk, "sitfis")
        assert head is not None
        revoke_authority(
            world.termo.actor,
            world.termo.client.pk,
            service="sitfis",
            expected_version=head.version,
        )
    with pytest.raises(module.SituationReportRefusedError):
        world.request()
    world.termo.tenant.features = ["cycles_r1", "scan_required"]
    world.termo.tenant.save(update_fields=["features"])
    with pytest.raises(module.SituationReportRefusedError):
        world.request()
    with tenant_context(world.tenant_id):
        assert not FiscalSituationReport.objects.exists()
        assert not FederalConsultation.objects.exists()
    assert world.calls("SP-SIT-P") == []
    assert world.gateway.requests == []


def test_a_repeated_request_returns_the_active_report(world: World) -> None:
    world.script(protocol_reply())
    first = world.request()
    again = world.request()
    assert again.pk == first.pk
    world.advance(first)
    assert world.request().pk == first.pk
    assert len(world.sent("/Apoiar")) == 1
    with tenant_context(world.tenant_id):
        assert FiscalSituationReport.objects.count() == 1


def _member(world: World, email: str) -> User:
    user = User.objects.create_user(email=email)
    # PLATFORM_QUERY_OK: a firm member of the synthetic tenant for scope tests.
    Membership.objects.create(
        tenant=world.termo.tenant, user=user, role=TenantRole.STAFF_ACCOUNTANT
    )
    return user


def test_staff_on_demand_is_scoped_to_the_portfolio(world: World) -> None:
    module = sitfis()
    outsider = _member(world, "fora-da-carteira@axrm016.example.test")
    with tenant_context(world.tenant_id), pytest.raises((Http404, PermissionDenied)):
        module.request_situation_report(world.termo.client, actor=outsider)
    with tenant_context(world.tenant_id):
        assert not FiscalSituationReport.objects.exists()
    world.script(protocol_reply())
    with tenant_context(world.tenant_id):
        report = module.request_situation_report(
            world.termo.client, actor=world.responsible
        )
    assert report.state == "solicitado"


def test_the_task_reschedules_itself_at_wait_until(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = __import__("tests.integrations.sitfis_support", fromlist=["module"]).module(
        "apps.integrations.serpro.sitfis_tasks"
    )
    scheduled: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        tasks.advance_situation_report_task,
        "apply_async",
        lambda args, **kwargs: scheduled.append((args, kwargs)),
    )
    world.script(protocol_reply(30_000))
    report = world.request()
    state = tasks.advance_situation_report_task(str(world.tenant_id), str(report.pk))
    assert state == "aguardando"
    [(args, kwargs)] = scheduled
    assert args == (str(world.tenant_id), str(report.pk))
    assert kwargs["eta"] == world.clock.now() + timedelta(seconds=30)
    # A final report schedules nothing more.
    scheduled.clear()
    world.clock.advance(seconds=30)
    world.script(report_reply())
    assert (
        tasks.advance_situation_report_task(str(world.tenant_id), str(report.pk))
        == "emitido"
    )
    assert scheduled == []


def test_the_emission_is_recorded_on_the_open_cycle_without_content(
    world: World,
) -> None:
    report = _protocol_received(world)
    world.clock.advance(seconds=30)
    world.script(report_reply())
    emitted = world.advance(report)
    with tenant_context(world.tenant_id):
        [event] = CycleEvent.objects.filter(type="federal.report.emitted")
    assert event.cycle_id == world.cycle.pk
    assert event.actor_id is None
    assert (event.aggregate_type, event.aggregate_id) == (
        "FiscalSituationReport",
        emitted.pk,
    )
    assert PROTOCOL not in json.dumps(event.payload)


def test_no_protocol_or_report_bytes_reach_the_logs(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    report = _protocol_received(world)
    world.clock.advance(seconds=30)
    world.script(report_reply())
    world.advance(report)
    text = "\n".join(
        f"{record.getMessage()} {record.__dict__}" for record in caplog.records
    )
    assert PROTOCOL not in text
    assert PDF.decode(errors="ignore")[:20] not in text
    assert "JVBERi" not in text  # base64 of "%PDF"


def test_scan_sweep_verifies_the_stored_report(world: World, clock: Clock) -> None:
    report = _protocol_received(world)
    world.clock.advance(seconds=30)
    world.script(report_reply())
    emitted = world.advance(report)
    world.scan()
    with tenant_context(world.tenant_id):
        intake = DocumentIntake.objects.get(document_id=emitted.document_id)
    assert intake.state == IntakeState.VERIFIED
