"""The SITFIS timing veto runs inside all three existing admission fences."""

import hashlib
import threading
from collections.abc import Callable
from datetime import timedelta
from importlib import import_module
from typing import Any

import pytest
from django.db import OperationalError, connection, connections, transaction
from django.db.models import F

from apps.audit.models import Event
from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleEvent, CycleState, ServiceCycle
from apps.cycles.models import Exception as CycleException
from apps.integrations.authority import head_record, revoke_authority
from apps.integrations.budget_models import ProviderReservation
from apps.integrations.call_contracts import CallRequest
from apps.integrations.call_dispatch import begin_io
from apps.integrations.call_models import ProviderCall, ProviderEffectBarrier
from apps.integrations.call_protocol import claim, reserve
from apps.integrations.models import (
    ClientProviderLink,
    IntegrationIssue,
    ProviderConnection,
)
from apps.integrations.serpro.codec import build_request
from apps.obligations.models import FederalConsultation, FiscalSituationReport
from tests.fakes.provider import Reply
from tests.integrations.sitfis_support import (
    EMISSAO_MICROBRL,
    World,
    clock,
    envelope,
    processing_202,
    protocol_reply,
    report_reply,
    sitfis,
    world,
)

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "world"]


def _ready(world: World) -> tuple[FiscalSituationReport, Any]:
    mod = sitfis()
    world.script(protocol_reply(30_000))
    report = world.advance(world.request())
    world.clock.advance(seconds=30)
    step = mod._decide(world.tenant_id, report.pk)
    assert step is not None
    return report, step


def _request(step: Any) -> CallRequest:  # noqa: ANN401 - private domain step
    body = build_request(step.operation, step.parties, step.data).body
    return CallRequest(
        step.operation, step.key, hashlib.sha256(body).hexdigest(), step.key
    )


@pytest.mark.parametrize("site", ["reserve", "claim", "perform"])
def test_a_late_wait_veto_preserves_the_unsent_identity_and_budget(
    world: World, monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    mod = sitfis()
    report, first = _ready(world)
    world.script(processing_202(0))
    # Stop before applying attempt 1, so the next decision yields attempt 2
    # without transmitting it. All admission and finalization remain real.
    mod._run(world.tenant_id, report.pk, first)
    step = mod._decide(world.tenant_id, report.pk)
    assert step is not None
    [received] = world.calls("SP-SIT-R")
    original = getattr(mod, site)

    def reply_before_admission(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        mod._receive(
            world.tenant_id,
            report.pk,
            received.pk,
            mod.SitfisReply(wait=timedelta(seconds=300), received_at=world.clock.now()),
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(mod, site, reply_before_admission)
    mod._run(world.tenant_id, report.pk, step)
    monkeypatch.setattr(mod, site, original)
    after = world.reload(report)
    assert after.state == "aguardando"
    assert after.emission_attempts == 1
    assert len(world.sent("/Emitir")) == 1
    calls = world.calls("SP-SIT-R")
    assert len(calls) == (1 if site == "reserve" else 2)
    if site != "reserve":
        unused = calls[-1]
        assert unused.state == "reservada"
        assert unused.attempts == 0
        assert unused.dispatch_started_at is None
        assert unused.lease_owner is None
        assert unused.lease_until is None
    with tenant_context(world.tenant_id):
        assert not ProviderEffectBarrier.objects.filter(
            business_effect_key=step.key
        ).exists()
    budget = world.budget()
    assert budget.known_liability == EMISSAO_MICROBRL
    assert budget.reserved == (0 if site == "reserve" else EMISSAO_MICROBRL)
    assert budget.uncertain_exposure == 0
    world.clock.advance(seconds=299)
    world.advance(report)
    assert len(world.sent("/Emitir")) == 1
    world.clock.advance(seconds=1)
    world.script(report_reply())
    final = world.advance(report)
    assert final.state == "emitido"
    assert final.emission_attempts == 2
    assert len(world.sent("/Emitir")) == 2
    assert len(world.calls("SP-SIT-R")) == 2
    assert world.budget().reserved == 0
    assert world.budget().known_liability == 2 * EMISSAO_MICROBRL


@pytest.mark.parametrize("site", ["reserve", "claim", "dispatch-start"])
@pytest.mark.parametrize("exception", ["domain-veto", "unexpected-error"])
def test_an_authorize_exception_propagates_and_rolls_back_each_protocol_caller(
    world: World, monkeypatch: pytest.MonkeyPatch, site: str, exception: str
) -> None:
    mod = sitfis()
    report, step = _ready(world)
    row = None
    ticket = None
    if site != "reserve":
        row = reserve(world.tenant_id, step.link_id, _request(step))
    if site == "dispatch-start":
        assert row is not None
        ticket = claim(world.tenant_id, row.pk)
        assert ticket is not None
    before_budget = world.budget()
    before = world.calls("SP-SIT-R")
    error_type = (
        mod.EmissionDeferredError if exception == "domain-veto" else RuntimeError
    )

    def veto(call: ProviderCall) -> None:
        raise error_type(call)

    monkeypatch.setattr(mod, "admit_emission", veto)

    def invoke() -> None:
        if site == "reserve":
            reserve(world.tenant_id, step.link_id, _request(step))
        elif site == "claim":
            assert row is not None
            claim(world.tenant_id, row.pk)
        else:
            assert ticket is not None
            begin_io(ticket)

    with pytest.raises(error_type):
        invoke()
    after = world.calls("SP-SIT-R")
    fields = ("state", "fence", "attempts", "lease_owner", "dispatch_started_at")
    assert [tuple(getattr(call, field) for field in fields) for call in after] == [
        tuple(getattr(call, field) for field in fields) for call in before
    ]
    assert world.budget().reserved == before_budget.reserved
    assert world.budget().known_liability == before_budget.known_liability
    assert world.sent("/Emitir") == []
    with tenant_context(world.tenant_id):
        assert not ProviderEffectBarrier.objects.filter(
            business_effect_key=step.key
        ).exists()
    assert world.reload(report).emission_attempts == 0


@pytest.mark.parametrize("site", ["reserve", "claim", "perform"])
def test_a_paid_pdf_veto_cancels_unsent_work_and_releases_its_budget(
    world: World, monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    mod = sitfis()
    report, first = _ready(world)
    world.script(Reply(body=report_reply().body, held=True))
    world.gateway.received.clear()
    errors: list[str] = []

    def slow_pdf() -> None:
        try:
            mod._run(world.tenant_id, report.pk, first)
        except BaseException as error:  # noqa: BLE001 - return thread failures
            errors.append(repr(error))
        finally:
            connections.close_all()

    a = threading.Thread(target=slow_pdf)
    a.start()
    try:
        assert world.gateway.received.wait(timeout=15)
        world.clock.advance(seconds=121)
        world.advance(report)
        world.clock.advance(seconds=60)
        step = mod._decide(world.tenant_id, report.pk)
        assert step is not None
        original = getattr(mod, site)

        def release_paid_reply(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            world.gateway.release.set()
            a.join(timeout=15)
            assert not a.is_alive()
            assert errors == []
            assert world.reload(report).document_id is not None
            return original(*args, **kwargs)

        monkeypatch.setattr(mod, site, release_paid_reply)
        mod._run(world.tenant_id, report.pk, step)
        monkeypatch.setattr(mod, site, original)
    finally:
        world.gateway.release.set()
        a.join(timeout=15)
    assert not a.is_alive()
    assert errors == []
    final = world.reload(report)
    assert final.state == "emitido"
    assert final.emission_attempts == 1
    assert len(world.sent("/Emitir")) == 1
    unused = world.calls("SP-SIT-R")[1:]
    assert len(unused) == (0 if site == "reserve" else 1)
    for call in unused:
        assert call.state == "cancelada"
        assert call.attempts == 0
        assert call.dispatch_started_at is None
    assert world.budget().reserved == 0
    world.advance(report)
    assert len(world.sent("/Emitir")) == 1


def _hold_old_202(
    world: World,
    first: object,
    report: FiscalSituationReport,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[threading.Thread, threading.Event, list[str]]:
    mod = sitfis()
    ready, release = threading.Event(), threading.Event()
    original = mod._receive
    errors: list[str] = []

    def receive(*args: Any) -> None:  # noqa: ANN401 - receive barrier
        if threading.current_thread() is not threading.main_thread():
            ready.set()
            assert release.wait(timeout=30)
        original(*args)

    def run() -> None:
        try:
            mod._run(world.tenant_id, report.pk, first)
        except BaseException as error:  # noqa: BLE001 - return thread failures
            errors.append(repr(error))
        finally:
            connections.close_all()

    monkeypatch.setattr(mod, "_receive", receive)
    runner = threading.Thread(target=run)
    runner.start()
    assert ready.wait(timeout=15)
    return runner, release, errors


@pytest.mark.parametrize("site", ["retry-claim", "retry-dispatch"])
def test_a_401_retry_veto_uses_its_actual_fence_and_resumes_the_same_identity(
    world: World, monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    from apps.integrations import call_protocol  # noqa: PLC0415

    report, first = _ready(world)
    world.script(processing_202(300_000))
    a, release, errors = _hold_old_202(world, first, report, monkeypatch)
    name = "schedule_client_retry" if site == "retry-claim" else "begin_io"
    original = getattr(call_protocol, name)

    def release_reply() -> None:
        release.set()
        a.join(timeout=15)
        assert not a.is_alive()
        assert errors == []

    def before_claim(*args: Any) -> Any:  # noqa: ANN401 - core retry boundary
        retry = original(*args)
        if retry:
            release_reply()
        return retry

    def before_dispatch(ticket: Any) -> Any:  # noqa: ANN401 - core retry boundary
        if ticket.fence == 2:
            release_reply()
        return original(ticket)

    try:
        world.clock.advance(seconds=121)
        world.advance(report)
        world.clock.advance(seconds=60)
        world.script(Reply(status=401, body=envelope(401, "")))
        hook = before_claim if site == "retry-claim" else before_dispatch
        monkeypatch.setattr(call_protocol, name, hook)
        paused = world.advance(report)
        monkeypatch.setattr(call_protocol, name, original)
    finally:
        release_reply()
    assert paused.state == "aguardando"
    assert paused.emission_attempts == 1
    assert len(world.sent("/Emitir")) == 2
    retry = world.calls("SP-SIT-R")[-1]
    expected = "aguardar_nova_tentativa" if site == "retry-claim" else "reservada"
    assert retry.state == expected
    assert retry.attempts == 1
    if site == "retry-dispatch":
        assert retry.lease_owner is None
        assert retry.dispatch_started_at is None
    world.clock.advance(seconds=119)
    world.script(report_reply())
    final = world.advance(report)
    assert final.state == "emitido"
    assert final.emission_attempts == 2
    assert len(world.calls("SP-SIT-R")) == 2
    assert len(world.sent("/Emitir")) == 3
    assert world.budget().reserved == 0
    world.advance(report)
    assert len(world.sent("/Emitir")) == 3


@pytest.mark.parametrize(
    "state",
    [
        "solicitado",
        "protocolo_recebido",
        "aguardando",
        "emitindo",
        "emitido",
        "negado",
        "falha",
        "incerto",
        "protocolo_expirado",
        "limite_de_tentativas",
    ],
)
def test_a_durable_paid_pdf_is_recovered_in_every_report_state(
    world: World, state: str
) -> None:
    mod = sitfis()
    report, step = _ready(world)
    world.script(report_reply())
    mod._run(world.tenant_id, report.pk, step)
    paid = world.reload(report)
    document_id = paid.document_id
    assert document_id is not None
    # Model the already-committed original from the pre-repair worker, before
    # _receive attached it. The acquisition receipt proves its exact paid call.
    with tenant_context(world.tenant_id):
        FiscalSituationReport.objects.filter(pk=report.pk).update(
            state=state, document=None, emitted_at=None, reply_call="", call_replies={}
        )
        if state in mod.FINAL_STATES and state != "emitido":
            mod._own(world.reload(report), "SP-SIT-R")
    recovered = world.advance(report)
    assert recovered.state == "emitido"
    assert recovered.document_id == document_id
    assert len(world.sent("/Emitir")) == 1
    with tenant_context(world.tenant_id):
        assert (
            FederalConsultation.objects.get(pk=report.consultation_id).outcome
            == "encontrado"
        )
        problems = CycleException.objects.filter(
            subject_ref=report.pk, reason_code=mod.REPORT_FAILED
        )
        assert not problems.exclude(state="resolvida").exists()
        if problems.exists():
            assert CycleEvent.objects.filter(
                type="exception.transitioned",
                payload__reason="relatorio_pago_adotado",
                payload__report_id=str(report.pk),
            ).exists()


def test_adopting_a_limit_pdf_resolves_the_issue_when_no_cycle_is_open(
    world: World,
) -> None:
    mod = sitfis()
    report, step = _ready(world)
    world.script(report_reply())
    mod._run(world.tenant_id, report.pk, step)
    with tenant_context(world.tenant_id):
        FiscalSituationReport.objects.filter(pk=report.pk).update(
            state="limite_de_tentativas", document=None, emitted_at=None, reply_call=""
        )
        ServiceCycle.objects.filter(pk=world.cycle.pk).update(
            state=CycleState.CANCELLED
        )
        mod._own(world.reload(report), "SP-SIT-R")
        issue = IntegrationIssue.objects.get(reason=mod.REPORT_FAILED)
        assert issue.state == "aberta"
    recovered = world.advance(report)
    assert recovered.state == "emitido"
    assert recovered.document_id is not None
    assert len(world.sent("/Emitir")) == 1
    with tenant_context(world.tenant_id):
        assert IntegrationIssue.objects.get(pk=issue.pk).state == "resolvida"
        assert Event.objects.filter(
            action="integrations.issue_resolved",
            object_id=str(issue.pk),
            metadata__resolution="relatorio_pago_adotado",
        ).exists()


def test_a_stale_claim_meeting_a_late_wait_is_refused_and_released(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = sitfis()
    report, first = _ready(world)
    world.script(processing_202(0))
    mod._run(world.tenant_id, report.pk, first)
    step = mod._decide(world.tenant_id, report.pk)
    assert step is not None
    [received] = world.calls("SP-SIT-R")
    original = mod.claim

    def late_wait_on_a_moved_version(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        # Given a 300-second wait arrives, and the connection version moves,
        # before this claim's admission.
        mod._receive(
            world.tenant_id,
            report.pk,
            received.pk,
            mod.SitfisReply(wait=timedelta(seconds=300), received_at=world.clock.now()),
        )
        with tenant_context(world.tenant_id):
            link = ClientProviderLink.objects.get(pk=received.link_id)
            ProviderConnection.objects.filter(pk=link.connection_id).update(
                version=F("version") + 1
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(mod, "claim", late_wait_on_a_moved_version)
    mod._run(world.tenant_id, report.pk, step)
    # Then the claim itself recorded the refusal and released the reservation,
    # never a deferral that keeps it held (AXRM-045).
    stale = world.calls("SP-SIT-R")[-1]
    assert (stale.state, stale.provider_code) == (
        "bloqueada_autoridade",
        "bloqueada_autoridade",
    )
    assert world.budget().reserved == 0
    assert len(world.sent("/Emitir")) == 1


def test_a_claim_refusal_closes_the_consultation_from_the_stored_call(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = sitfis()
    report, first = _ready(world)
    original = mod.claim

    def absent_on_a_moved_version(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        # Given the client's authority is revoked and the connection version
        # moves before this claim, so the claim records `autorizacao_ausente`...
        with tenant_context(world.tenant_id):
            head = head_record(world.termo.client.pk, "sitfis")
            assert head is not None
            revoke_authority(
                world.termo.actor,
                world.termo.client.pk,
                service="sitfis",
                expected_version=head.version,
            )
            link = ClientProviderLink.objects.get(pk=first.link_id)
            ProviderConnection.objects.filter(pk=link.connection_id).update(
                version=F("version") + 1
            )
        try:
            return original(*args, **kwargs)
        finally:
            # ...and the link is disabled once that refusal has committed.
            with tenant_context(world.tenant_id):
                ClientProviderLink.objects.filter(pk=first.link_id).update(
                    status="revogada"
                )

    monkeypatch.setattr(mod, "claim", absent_on_a_moved_version)
    mod._run(world.tenant_id, report.pk, first)
    # Then the report settles from the refusal stored on the call, classified
    # against that call's own link, and its receipt names that call (AXRM-045).
    refused = world.calls(first.operation)[-1]
    assert (refused.state, refused.provider_code) == (
        "bloqueada_autoridade",
        "autorizacao_ausente",
    )
    settled = world.reload(report)
    with tenant_context(world.tenant_id):
        receipt = FederalConsultation.objects.get(pk=settled.consultation_id)
    assert (settled.state, receipt.provider_ref) == ("negado", str(refused.pk))
    assert world.budget().reserved == 0
    assert world.sent("/Emitir") == []


def _hold(
    world: World, lock: Callable[[], object]
) -> tuple[threading.Thread, threading.Event, list[str]]:
    """Hold one row lock on a second connection until released."""
    held, release = threading.Event(), threading.Event()
    errors: list[str] = []

    def run() -> None:
        try:
            with tenant_context(world.tenant_id), transaction.atomic():
                lock()
                held.set()
                assert release.wait(timeout=30)
        except BaseException as error:  # noqa: BLE001 - return thread failures
            errors.append(repr(error))
        finally:
            connections.close_all()

    holder = threading.Thread(target=run)
    holder.start()
    assert held.wait(timeout=15)
    return holder, release, errors


def _refuse_at_claim_while(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    lock: Any,  # noqa: ANN401
) -> tuple[FiscalSituationReport, Any, list[str]]:
    """Run one claim whose refusal commits, then hold `lock` before SITFIS settles."""
    mod = sitfis()
    report, first = _ready(world)
    original = mod.claim
    holders: list[tuple[threading.Thread, threading.Event, list[str]]] = []

    def refused_then_locked(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        with tenant_context(world.tenant_id):
            link = ClientProviderLink.objects.get(pk=first.link_id)
            ProviderConnection.objects.filter(pk=link.connection_id).update(
                version=F("version") + 1
            )
        try:
            return original(*args, **kwargs)
        finally:
            holders.append(_hold(world, lambda: lock(first)))

    monkeypatch.setattr(mod, "claim", refused_then_locked)
    mod._run(world.tenant_id, report.pk, first)
    monkeypatch.setattr(mod, "claim", original)
    [(holder, release, errors)] = holders
    release.set()
    holder.join(timeout=30)
    return report, first, errors


def _lock_connection(first: Any) -> None:  # noqa: ANN401
    link = ClientProviderLink.objects.get(pk=first.link_id)
    list(ProviderConnection.objects.select_for_update().filter(pk=link.connection_id))


def _lock_call(first: Any) -> None:  # noqa: ANN401
    list(ProviderCall.objects.select_for_update().filter(request_key=first.key))


def test_a_lock_on_the_connection_never_hides_a_committed_claim_refusal(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the claim commits a refusal, and another worker then holds the
    # connection's row lock while SITFIS settles it...
    report, first, errors = _refuse_at_claim_while(world, monkeypatch, _lock_connection)
    assert errors == []
    # Then the report still closes from the stored call, never with a blank ref.
    refused = world.calls(first.operation)[-1]
    assert refused.state == "bloqueada_autoridade"
    settled = world.reload(report)
    with tenant_context(world.tenant_id):
        receipt = FederalConsultation.objects.get(pk=settled.consultation_id)
    assert (settled.state, receipt.provider_ref) == ("falha", str(refused.pk))


def test_a_locked_refused_call_defers_its_report_and_never_closes_it_blank(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the claim commits a refusal, and another worker then holds that
    # call's own row lock while SITFIS settles it...
    report, first, errors = _refuse_at_claim_while(world, monkeypatch, _lock_call)
    assert errors == []
    # Then that pass defers: the report stays emitting, its receipt unwritten...
    deferred = world.reload(report)
    with tenant_context(world.tenant_id):
        receipt = FederalConsultation.objects.get(pk=deferred.consultation_id)
    assert (deferred.state, receipt.provider_ref) == ("emitindo", "")
    # ...and the next pass settles it from the stored call.
    refused = world.calls(first.operation)[-1]
    settled = world.advance(deferred)
    with tenant_context(world.tenant_id):
        receipt = FederalConsultation.objects.get(pk=settled.consultation_id)
    assert (settled.state, receipt.provider_ref) == ("falha", str(refused.pk))


def _hold_the_budget_lock(
    world: World,
) -> tuple[threading.Thread, threading.Event, threading.Event, list[str]]:
    """One connection holds the cross-firm budget lock until released."""
    holding, done = threading.Event(), threading.Event()
    failures: list[str] = []

    def hold() -> None:
        try:
            with tenant_context(world.tenant_id), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '10s'")
                    cursor.execute("SELECT pg_advisory_xact_lock(%s)", [240110])
                holding.set()
                assert done.wait(timeout=30)
        except OperationalError as error:
            failures.append(f"holder: {error}")
        finally:
            connections.close_all()

    return threading.Thread(target=hold), holding, done, failures


def _signal_the_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[threading.Event, threading.Event, threading.Event, list[bool]]:
    """Signal, in the claiming thread only: the probe reached the report, the
    release began with the budget lock taken elsewhere, and the release ended."""
    mod = sitfis()
    state_module = import_module("apps.integrations.call_state")
    real_admit, real_release = mod.admit_emission, state_module.release
    probed, releasing, released = (threading.Event() for _ in range(3))
    lock_free_at_release: list[bool] = []

    def in_claimer() -> bool:
        return threading.current_thread() is not threading.main_thread()

    def admit_then_signal(call: Any) -> None:  # noqa: ANN401
        real_admit(call)
        if in_claimer():
            probed.set()

    def release_after_signal(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if not in_claimer():
            return real_release(*args, **kwargs)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [240110])
            lock_free_at_release.append(cursor.fetchone()[0])
        releasing.set()
        try:
            return real_release(*args, **kwargs)
        finally:
            released.set()

    monkeypatch.setattr(mod, "admit_emission", admit_then_signal)
    monkeypatch.setattr(state_module, "release", release_after_signal)
    return probed, releasing, released, lock_free_at_release


def test_a_moved_claims_reread_holds_no_report_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, first = _ready(world)
    reserved_before = world.budget().reserved
    # Given a reserved emission whose connection version then moves...
    row = reserve(world.tenant_id, first.link_id, _request(first))
    with tenant_context(world.tenant_id):
        link = ClientProviderLink.objects.get(pk=first.link_id)
        ProviderConnection.objects.filter(pk=link.connection_id).update(
            version=F("version") + 1
        )
    # ...and the budget lock is held elsewhere, so the moved claim's refusal
    # stops in `release` after its re-read reached the report.
    holder, holding, done, failures = _hold_the_budget_lock(world)
    holder.start()
    assert holding.wait(timeout=15)
    probed, releasing, released, lock_free_at_release = _signal_the_claim(monkeypatch)
    refusals: list[str] = []

    def claim_it() -> None:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '10s'")
            claim(world.tenant_id, row.pk)
        except Exception as error:  # noqa: BLE001 - only the moved refusal may end it
            refusals.append(f"{type(error).__name__}: {error}")
        finally:
            connections.close_all()

    claimer = threading.Thread(target=claim_it)
    claimer.start()
    assert releasing.wait(timeout=15)
    # When this runner locks the report while that claim is still in release.
    window: dict[str, bool] = {}
    try:
        with tenant_context(world.tenant_id), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '5s'")
            FiscalSituationReport.objects.select_for_update().get(pk=report.pk)
            window = {
                "probed": probed.is_set(),
                "claiming": claimer.is_alive(),
                "released": released.is_set(),
            }
        locked = "locked"
    except OperationalError as error:
        locked = f"waited: {error}"
    finally:
        done.set()
        holder.join(timeout=30)
        claimer.join(timeout=30)
    # Then the re-read kept no new lock: it only named a cause (AXRM-045).
    assert locked == "locked"
    # The probe had reached the report and the release was waiting on the budget
    # lock held elsewhere: this session took the report before that lock went.
    assert window == {"probed": True, "claiming": True, "released": False}
    assert lock_free_at_release == [False]
    assert (holder.is_alive(), claimer.is_alive()) == (False, False)
    assert failures == []
    assert refusals == ["VersionMovedError: bloqueada_autoridade"]
    _assert_refused_and_released(world, first.operation, row.pk, reserved_before)


def _assert_refused_and_released(
    world: World,
    operation: str,
    call_id: object,
    reserved_before: int,
) -> None:
    """The refusal committed: the call names its cause, its slot is released,
    the budget is back where it was and nothing was sent."""
    refused = world.calls(operation)[-1]
    assert (refused.pk, refused.state, refused.provider_code) == (
        call_id,
        "bloqueada_autoridade",
        "bloqueada_autoridade",
    )
    with tenant_context(world.tenant_id):
        slots = ProviderReservation.objects.filter(call=refused)
        assert list(slots.values_list("attempt_number", "state")) == [(1, "released")]
    assert world.budget().reserved == reserved_before
    assert world.sent("/Emitir") == []
