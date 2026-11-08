"""A final wire veto leaves durable evidence, zero spend and a reusable slot."""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from django.test import override_settings

from apps.core.tenancy import tenant_context
from apps.integrations import (
    budget_settlement,
    call_contracts,
    call_dispatch,
    call_protocol,
    provider_journal,
    transport,
    transport_wire,
)
from apps.integrations import call_unopened as unopened
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.call_contracts import CallResult, operation_for
from apps.integrations.call_models import (
    CallState,
    ProviderCall,
    ProviderCallAttempt,
    ProviderEffectBarrier,
)
from apps.integrations.journal_schema import (
    FirmJournalRecord,
    JournalError,
    JournalRecord,
)
from apps.integrations.models import ACTIVE_ISSUE_STATES, IntegrationIssue
from apps.integrations.provider_replay import _restore, reconcile
from apps.integrations.transport_types import EnvioNaoAutorizado
from apps.tenants.models import UsageRecord
from tests.integrations.sitfis_support import (
    EMISSAO_MICROBRL,
    World,
    clock,
    protocol_reply,
    report_reply,
    sitfis,
    world,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from apps.integrations.call_dispatch import Adapter, Dispatch
    from apps.obligations.models import FiscalSituationReport

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "world"]


@dataclass
class Veto:
    world: World
    report: FiscalSituationReport
    dispatches: list[Dispatch]

    def run(self) -> FiscalSituationReport:
        return self.world.advance(self.report)


@pytest.fixture
def veto(world: World, monkeypatch: pytest.MonkeyPatch) -> Veto:
    mod = sitfis()
    world.script(protocol_reply(30_000))
    report = world.advance(world.request())
    world.clock.advance(seconds=30)
    world.script(report_reply())
    case = Veto(world, report, [])
    real_transmit = call_dispatch.transmit

    def late_wait(dispatch: Dispatch, adapter: Adapter) -> CallResult:
        if not case.dispatches:
            # A controlled receipt commits after dispatch-start, before admission.
            mod._receive(
                world.tenant_id,
                report.pk,
                dispatch.claim.call_id,
                mod.SitfisReply(
                    received_at=world.clock.now(), wait=timedelta(seconds=300)
                ),
            )
        case.dispatches.append(dispatch)
        return real_transmit(dispatch, adapter)

    monkeypatch.setattr(call_protocol, "transmit", late_wait)
    return case


def _unused(case: Veto) -> ProviderCall:
    assert case.world.sent("/Emitir") == []
    with tenant_context(case.world.tenant_id):
        [call] = ProviderCall.objects.filter(operation="SP-SIT-R")
        assert call.state == CallState.RETRY
        assert call.attempts == 0
        assert call.reserved_microbrl == 0
        assert call.dispatch_started_at is None
        assert call.lease_owner is None
        assert call.lease_until is None
        assert not ProviderReservation.objects.filter(call=call).exists()
        assert (
            ProviderCallAttempt.objects.filter(
                call=call, observation="result", provider_code="wire_veto"
            ).count()
            == 1
        )
        budget = ProviderBudget.objects.get(provider="serpro")
        assert (budget.known_liability, budget.uncertain_exposure, budget.reserved) == (
            0,
            0,
            0,
        )
        assert not IntegrationIssue.objects.filter(
            state__in=ACTIVE_ISSUE_STATES, evidence_refs__contains=[str(call.pk)]
        ).exists()
        assert (
            sum(
                UsageRecord.objects.filter(
                    metric="serpro_emissao", corrections__isnull=True
                ).values_list("quantity", flat=True)
            )
            == 0
        )
        return call


def test_veto_reuses_the_same_logical_slot_and_keeps_the_effect_barrier(
    veto: Veto, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = veto.run()
    assert final.state == "aguardando"
    assert final.emission_attempts == 0
    before = _unused(veto)
    records = [
        record
        for record in provider_journal.read(veto.world.tenant_id)
        if record.call_id == before.pk
    ]
    with tenant_context(veto.world.tenant_id):
        barrier = ProviderEffectBarrier.objects.get(call=before)
        identity = (barrier.pk, barrier.first_start_receipt, barrier.request_digest)
    real_transmit = call_dispatch.transmit

    def old_replay_during_the_new_start(
        dispatch: Dispatch, adapter: Adapter
    ) -> CallResult:
        with tenant_context(veto.world.tenant_id):
            _restore(records)
            current = ProviderCall.objects.get(pk=before.pk)
            assert current.fence == dispatch.claim.fence
            assert current.lease_owner == dispatch.claim.owner
            assert current.state == CallState.STARTED
            held = ProviderReservation.objects.get(call=current)
            assert held.state == "reserved"
            assert held.microbrl == EMISSAO_MICROBRL
            assert (
                ProviderBudget.objects.get(provider="serpro").reserved == held.microbrl
            )
        return real_transmit(dispatch, adapter)

    monkeypatch.setattr(call_protocol, "transmit", old_replay_during_the_new_start)
    veto.world.clock.advance(seconds=300)
    assert veto.run().state == "emitido"
    assert len(veto.world.sent("/Emitir")) == 1
    with tenant_context(veto.world.tenant_id):
        [after] = ProviderCall.objects.filter(operation="SP-SIT-R")
        assert after.pk == before.pk
        assert after.request_key == before.request_key
        assert after.business_effect_key == before.business_effect_key
        assert after.attempts == 1
        assert after.fence > before.fence
        barrier = ProviderEffectBarrier.objects.get(call=after)
        assert (
            barrier.pk,
            barrier.first_start_receipt,
            barrier.request_digest,
        ) == identity
        budget = ProviderBudget.objects.get(provider="serpro")
        assert (budget.known_liability, budget.uncertain_exposure, budget.reserved) == (
            EMISSAO_MICROBRL,
            0,
            0,
        )
        assert (
            sum(
                UsageRecord.objects.filter(
                    metric="serpro_emissao", corrections__isnull=True
                ).values_list("quantity", flat=True)
            )
            == 1
        )


@pytest.mark.parametrize("variant", ["clone", "fence", "call", "owner", "journal"])
@pytest.mark.parametrize("entrypoint", ["result", "abort"])
def test_only_the_exact_issued_dispatch_can_publish_a_marker(
    veto: Veto, monkeypatch: pytest.MonkeyPatch, variant: str, entrypoint: str
) -> None:
    mod = sitfis()
    original = mod.abort_unopened

    def forged(dispatch: Dispatch) -> None:
        claims = {
            "clone": dispatch.claim,
            "fence": replace(dispatch.claim, fence=dispatch.claim.fence + 1),
            "call": replace(dispatch.claim, call_id=uuid4()),
            "owner": replace(dispatch.claim, owner=uuid4()),
        }
        other = (
            replace(dispatch, journaled=False)
            if variant == "journal"
            else replace(dispatch, claim=claims[variant])
        )
        if entrypoint == "result":
            with pytest.raises(EnvioNaoAutorizado):
                call_dispatch.record_result(other, unopened.UNOPENED_RESULT)
        else:
            with pytest.raises(EnvioNaoAutorizado):
                call_dispatch.abort_unopened(other)
        original(dispatch)

    monkeypatch.setattr(mod, "abort_unopened", forged)
    veto.run()
    _unused(veto)


def test_a_second_marker_in_the_same_admission_is_rejected(
    veto: Veto, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = sitfis()
    original = mod.abort_unopened

    def duplicate(dispatch: Dispatch) -> None:
        original(dispatch)
        with pytest.raises(EnvioNaoAutorizado):
            call_dispatch.record_result(dispatch, unopened.UNOPENED_RESULT)
        with pytest.raises(EnvioNaoAutorizado):
            call_dispatch.abort_unopened(replace(dispatch, journaled=False))

    monkeypatch.setattr(mod, "abort_unopened", duplicate)
    veto.run()
    _unused(veto)


def test_direct_public_append_cannot_forge_an_admission_marker(
    veto: Veto, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = sitfis()
    original = mod.abort_unopened

    def direct(dispatch: Dispatch) -> None:
        with pytest.raises(JournalError):
            provider_journal.append(
                replace(dispatch.receipt, result=unopened.UNOPENED_RESULT)
            )
        original(dispatch)

    monkeypatch.setattr(mod, "abort_unopened", direct)
    veto.run()
    _unused(veto)


def test_a_copied_context_cannot_reuse_the_public_writer_capability(
    veto: Veto, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = provider_journal.append
    checked: list[str] = []

    def copied(record: JournalRecord | FirmJournalRecord) -> str:
        if not unopened.is_unopened(record.operation, record.result):
            return original(record)
        inherited = copy_context()
        path = original(record)
        with pytest.raises(JournalError):
            inherited.run(original, record)
        with pytest.raises(JournalError):
            original(record)
        checked.append(path)
        return path

    monkeypatch.setattr(provider_journal, "append", copied)
    veto.run()
    assert len(checked) == 1
    _unused(veto)


def test_marker_apis_reject_a_scope_that_has_already_closed(veto: Veto) -> None:
    veto.run()
    [dispatch] = veto.dispatches
    with pytest.raises(EnvioNaoAutorizado):
        call_dispatch.record_result(dispatch, unopened.UNOPENED_RESULT)
    with pytest.raises(JournalError):
        provider_journal.append(
            replace(dispatch.receipt, result=unopened.UNOPENED_RESULT)
        )
    _unused(veto)


def test_a_forked_context_cannot_reuse_the_public_writer_capability(
    veto: Veto,
) -> None:
    veto.run()
    [dispatch] = veto.dispatches
    record = replace(dispatch.receipt, result=unopened.UNOPENED_RESULT)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import django; django.setup(); "
                "from tests.integrations.sitfis_marker_fork import main; main()"
            ),
        ],
        input=record.encode(),
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert b"FORKED_MARKER_REJECTED_PARENT_CAPABILITY_CONSUMED_ONCE" in result.stdout
    _unused(veto)


def test_an_inherited_dispatch_scope_requires_the_original_process(
    veto: Veto, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = sitfis()
    original = mod.abort_unopened
    process_id = os.getpid()

    def inherited(dispatch: Dispatch) -> None:
        with monkeypatch.context() as child:
            child.setattr(call_dispatch, "getpid", lambda: process_id + 1)
            with pytest.raises(EnvioNaoAutorizado):
                call_dispatch.record_result(dispatch, unopened.UNOPENED_RESULT)
        original(dispatch)

    monkeypatch.setattr(mod, "abort_unopened", inherited)
    veto.run()
    _unused(veto)


def _after_call[**P, T](
    callback: Callable[P, T], inspect: Callable[[], None]
) -> Callable[P, T]:
    def observed(*args: P.args, **kwargs: P.kwargs) -> T:
        reply = callback(*args, **kwargs)
        inspect()
        return reply

    return observed


def test_markers_are_rejected_after_real_bytes_while_the_wire_scope_is_open(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.script(protocol_reply(30_000))
    report = world.advance(world.request())
    world.clock.advance(seconds=30)
    world.script(report_reply())
    dispatches: list[Dispatch] = []
    checked: list[str] = []

    def capture(dispatch: Dispatch, adapter: Adapter) -> CallResult:
        dispatches.append(dispatch)
        return call_dispatch.transmit(dispatch, adapter)

    def after_bytes() -> None:
        assert len(world.sent("/Emitir")) == 1
        [dispatch] = dispatches
        with pytest.raises(EnvioNaoAutorizado):
            call_dispatch.record_result(dispatch, unopened.UNOPENED_RESULT)
        with pytest.raises(JournalError):
            provider_journal.append(
                replace(dispatch.receipt, result=unopened.UNOPENED_RESULT)
            )
        checked.append("rejected_after_real_bytes")

    monkeypatch.setattr(call_protocol, "transmit", capture)
    monkeypatch.setattr(
        transport,
        "_exchange",
        _after_call(transport_wire._exchange, after_bytes),
    )
    assert world.advance(report).state == "emitido"
    assert checked == ["rejected_after_real_bytes"]


def test_a_marker_followed_by_a_normal_guard_return_cannot_send(
    veto: Veto, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def returns_normally(dispatch: Dispatch) -> Iterator[None]:
        call_dispatch.abort_unopened(dispatch)
        yield

    original = operation_for("SP-SIT-R")
    monkeypatch.setitem(
        call_contracts._OPERATIONS,
        original.code,
        replace(original, wire_guard=returns_normally),
    )
    try:
        with pytest.raises(EnvioNaoAutorizado):
            veto.run()
    finally:
        assert veto.world.sent("/Emitir") == []
    _unused(veto)


def test_unguarded_sitfis_is_rejected_before_any_network_access(
    veto: Veto, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = operation_for("SP-SIT-R")
    monkeypatch.setitem(
        call_contracts._OPERATIONS,
        original.code,
        replace(original, wire_guard=None),
    )
    with pytest.raises(EnvioNaoAutorizado):
        veto.run()
    assert veto.world.sent("/Emitir") == []
    assert not any(
        unopened.is_unopened(record.operation, record.result)
        for record in provider_journal.read(veto.world.tenant_id)
    )


class CrashCutError(Exception):
    """Stop the worker at an intentional durable/transactional crash boundary."""


@pytest.mark.parametrize("cut", ["after_journal", "during_rewind"])
def test_crash_after_marker_or_during_sql_rewind_recovers_exactly(
    veto: Veto, monkeypatch: pytest.MonkeyPatch, cut: str
) -> None:
    original_record = call_dispatch.record_result
    original_release = budget_settlement.release_unsent

    def after_journal(dispatch: Dispatch, result: CallResult) -> None:
        original_record(dispatch, result)
        if unopened.is_unopened(dispatch.receipt.operation, result):
            raise CrashCutError

    def during_rewind(record: JournalRecord) -> None:
        original_release(record)
        raise CrashCutError

    if cut == "after_journal":
        monkeypatch.setattr(call_dispatch, "record_result", after_journal)
    else:
        monkeypatch.setattr(unopened, "release_unsent", during_rewind)
    with pytest.raises(CrashCutError):
        veto.run()
    assert veto.world.sent("/Emitir") == []
    records = provider_journal.read(veto.world.tenant_id)
    assert any(unopened.is_unopened(row.operation, row.result) for row in records)
    with tenant_context(veto.world.tenant_id):
        call = ProviderCall.objects.get(operation="SP-SIT-R")
        assert call.state == CallState.STARTED
        assert call.attempts == 1
        assert (
            ProviderBudget.objects.get(provider="serpro").reserved == EMISSAO_MICROBRL
        )
        assert ProviderReservation.objects.get(call=call).state == "reserved"
    monkeypatch.setattr(unopened, "release_unsent", original_release)
    monkeypatch.setattr(call_dispatch, "record_result", original_record)
    with override_settings(PROVIDERS_PAUSED=True):
        reconcile(uuid4())
        reconcile(uuid4())
    veto.run()
    _unused(veto)
