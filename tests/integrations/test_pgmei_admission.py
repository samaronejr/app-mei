"""Exercise the guide hook through real admission, money and competing runners."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, local
from typing import TYPE_CHECKING

import pytest
from django.db import DatabaseError, connection, connections
from django.utils import timezone

from apps.audit.services import ObjectRef, record_event
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.integrations import call_protocol
from apps.integrations.authority import Admission, AuthorityDecision
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_state import lock_controls
from apps.integrations.serpro import pgmei, registration
from apps.integrations.serpro.adapter import SerproAdapter
from apps.integrations.serpro.reauthentication import schedule_client_retry
from apps.integrations.transport import ProviderTransport
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations.pgmei_support import (
    GUIDE_PDF,
    PRICE_MICROBRL,
    GuideWorld,
    das_reply,
    guide_world,
)
from tests.integrations.test_pgmei_recovery import (
    expire_lease,
    guide_calls,
    links,
    next_day,
    obtain,
    sweep,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from apps.integrations.call_contracts import CallResult, Claim
    from apps.integrations.call_dispatch import Dispatch
    from apps.integrations.models import ClientProviderLink
    from apps.tenants.models import Tenant

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["guide_world"]

PHASES = [
    ("reserve", CallState.QUEUED, 0),
    ("claim", CallState.RESERVED, 0),
    ("dispatch", CallState.CLAIMED, 0),
    ("retry-claim", CallState.RETRY, 1),
    ("retry-dispatch", CallState.CLAIMED, 1),
]


def reserve_on(
    world: GuideWorld, monkeypatch: pytest.MonkeyPatch, day_offset: int
) -> tuple[ProviderCall, pgmei._Prepared]:
    """Prepare a real phase fixture without the service's preliminary barrier."""
    day = timezone.localdate() + timedelta(days=day_offset)
    with monkeypatch.context() as patcher:
        patcher.setattr(pgmei, "consolidation_day", lambda: day)
        with tenant_context(world.tenant_id):
            prepared = pgmei._prepare(world.owner, world.tenant_id, world.cycle.pk)
            record_event(
                action=pgmei.REQUESTED_EVENT,
                actor=world.owner,
                tenant_id=world.tenant_id,
                obj=ObjectRef("ServiceCycle", str(world.cycle.pk)),
                metadata={"request_key": prepared.request.request_key},
            )
    return (
        call_protocol.reserve(world.tenant_id, prepared.link_id, prepared.request),
        prepared,
    )


def adapter_for(
    world: GuideWorld, prepared: pgmei._Prepared, origin: str
) -> pgmei.GuideAdapter:
    return pgmei.GuideAdapter(
        SerproAdapter(
            ProviderTransport("serpro", "fake", origin),
            prepared.parties,
            prepared.data,
        ),
        world.tenant_id,
        prepared.client_id,
        prepared.competence,
        prepared.day,
    )


def runtime_thread(action: Callable[[], str]) -> str:
    """Give either competing runner a real, bounded runtime-role connection."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")
            cursor.execute("SET statement_timeout = '10s'")
        return action()
    finally:
        connections.close_all()


@pytest.mark.parametrize(("phase", "state", "attempts"), PHASES)
def test_hook_veto_closes_unsent_work_without_burning_an_attempt_or_budget(
    guide_world: GuideWorld,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    state: str,
    attempts: int,
) -> None:
    world = guide_world
    real_admission = pgmei.guide_admission
    seen: list[tuple[str, int]] = []

    def veto(call: ProviderCall) -> bool:
        if call.state == state and call.attempts == attempts:
            seen.append((call.state, call.attempts))
            return False
        return real_admission(call)

    replies = (Reply(status=401), das_reply()) if attempts else (das_reply(),)
    with FakeSerpro(replies) as peer:
        with monkeypatch.context() as patcher:
            patcher.setattr(pgmei, "guide_admission", veto)
            with pytest.raises(pgmei.GuideRefused) as refused:
                obtain(world, peer.origin)
        assert refused.value.code == "sem_autorizacao"
        assert seen == [(state, attempts)]
        assert len(peer.requests) == attempts
        calls = guide_calls(world)
        assert len(calls) == (0 if phase == "reserve" else 1)
        for call in calls:
            assert call.attempts == attempts
            assert call.state in {CallState.CANCELLED, CallState.AUTHORITY}
            assert call_protocol.claim(world.tenant_id, call.pk) is None
        with tenant_context(world.tenant_id):
            budget = ProviderBudget.objects.get()
            assert (
                budget.reserved,
                budget.known_liability,
                budget.uncertain_exposure,
            ) == (
                0,
                0,
                0,
            )
            assert not ProviderReservation.objects.exclude(state="released").exists()
        assert links(world) == 0
        next_day(monkeypatch)
        assert obtain(world, peer.origin).code == "guia_obtida"
        assert len(peer.requests) == attempts + 1
        with tenant_context(world.tenant_id):
            budget = ProviderBudget.objects.get()
            assert budget.known_liability == PRICE_MICROBRL
            assert budget.reserved == 0


class HookFailureError(RuntimeError):
    """An unexpected domain-hook failure must not be swallowed by any caller."""


@pytest.mark.parametrize(("phase", "state", "attempts"), PHASES)
def test_hook_exception_propagates_and_its_rolled_back_phase_remains_recoverable(
    guide_world: GuideWorld,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    state: str,
    attempts: int,
) -> None:
    world = guide_world
    real_admission = pgmei.guide_admission
    failure = HookFailureError("synthetic guide admission failure")

    def fail(call: ProviderCall) -> bool:
        if call.state == state and call.attempts == attempts:
            raise failure
        return real_admission(call)

    replies = (Reply(status=401), das_reply()) if attempts else (das_reply(),)
    with FakeSerpro(replies) as peer:
        with monkeypatch.context() as patcher:
            patcher.setattr(pgmei, "guide_admission", fail)
            with pytest.raises(HookFailureError) as raised:
                obtain(world, peer.origin)
        assert raised.value is failure
        assert len(peer.requests) == attempts
        calls = guide_calls(world)
        assert len(calls) == (0 if phase == "reserve" else 1)
        for call in calls:
            assert (call.state, call.attempts) == (state, attempts)
            expire_lease(world, call)
        sweep(world, days=8)
        with tenant_context(world.tenant_id):
            assert ProviderBudget.objects.get().reserved == 0
            assert not ProviderReservation.objects.filter(state="reserved").exists()
        assert links(world) == 0
        next_day(monkeypatch)
        assert obtain(world, peer.origin).code == "guia_obtida"
        assert len(peer.requests) == attempts + 1


def test_dispatch_rechecks_a_predecessor_after_the_candidate_claim(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = guide_world
    real_admission = pgmei.guide_admission
    real_perform = call_protocol.perform
    real_send = pgmei.GuideAdapter.send
    retry: list[Claim] = []
    predecessor_states_at_send: list[str] = []
    with FakeSerpro(
        (
            Reply(status=401),
            das_reply(pdf=GUIDE_PDF + b"% first paid response\n"),
            das_reply(pdf=GUIDE_PDF + b"% forbidden second paid response\n"),
        )
    ) as peer:
        predecessor, old = reserve_on(world, monkeypatch, 0)
        first = call_protocol.claim(world.tenant_id, predecessor.pk)
        assert first is not None
        old_adapter = adapter_for(world, old, peer.origin)
        rejected = call_protocol._perform_once(first, old_adapter)
        candidate, prepared = reserve_on(world, monkeypatch, 1)

        def initial_snapshot(call: ProviderCall) -> bool:
            # Give the candidate an already-committed claim. Dispatch must not
            # trust that earlier decision when the first 401 reopens afterward.
            if call.pk == candidate.pk and call.state == CallState.RESERVED:
                return True
            return real_admission(call)

        def reopen_then_perform(
            ticket: Claim, adapter: pgmei.GuideAdapter
        ) -> CallResult:
            assert schedule_client_retry(first, rejected)
            claimed = call_protocol.claim(world.tenant_id, predecessor.pk)
            assert claimed is not None
            retry.append(claimed)
            return real_perform(ticket, adapter)

        def observe(adapter: pgmei.GuideAdapter, dispatch: Dispatch) -> CallResult:
            if dispatch.claim.call_id == candidate.pk:
                with tenant_context(world.tenant_id):
                    predecessor_states_at_send.append(
                        ProviderCall.objects.get(pk=predecessor.pk).state
                    )
            return real_send(adapter, dispatch)

        with monkeypatch.context() as patcher:
            patcher.setattr(pgmei, "guide_admission", initial_snapshot)
            patcher.setattr(pgmei, "perform", reopen_then_perform)
            patcher.setattr(pgmei.GuideAdapter, "send", observe)
            waiting = pgmei._send(
                candidate,
                prepared,
                actor=world.owner,
                tenant_id=world.tenant_id,
                origin=peer.origin,
            )
        real_perform(retry[0], old_adapter)
        pgmei.settle_guide(world.tenant_id, predecessor.pk)
        with tenant_context(world.tenant_id):
            budget = ProviderBudget.objects.get()
            assert budget.known_liability <= PRICE_MICROBRL
            assert budget.reserved == 0
            saved = ProviderCall.objects.get(pk=candidate.pk)
        assert not predecessor_states_at_send, (
            "candidate bought a guide after its predecessor reopened before dispatch"
        )
        assert waiting == pgmei.GuideOutcome("em_andamento", predecessor.pk)
        assert saved.state == CallState.CANCELLED
        assert saved.attempts == 0
        assert saved.lease_owner is None
        assert len(peer.requests) == 2
        assert links(world) == 1


def test_admission_and_settlement_share_the_controls_call_client_lock_order(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = guide_world
    admitting = Event()
    settling = Event()
    worker = local()
    real_admission = pgmei.guide_admission
    real_controls = lock_controls
    with FakeSerpro((Reply(status=401), das_reply())) as peer:
        predecessor, old = reserve_on(world, monkeypatch, 0)
        first = call_protocol.claim(world.tenant_id, predecessor.pk)
        assert first is not None
        call_protocol._perform_once(first, adapter_for(world, old, peer.origin))
        candidate, prepared = reserve_on(world, monkeypatch, 1)

        def admission(call: ProviderCall) -> bool:
            if call.pk == candidate.pk:
                admitting.set()
                assert settling.wait(timeout=10)
            return real_admission(call)

        def controls(
            tenant_id: UUID, link_id: UUID
        ) -> tuple[Tenant, ClientProviderLink]:
            if getattr(worker, "kind", "") == "settlement":
                settling.set()
            return real_controls(tenant_id, link_id)

        def claim_runner() -> str:
            try:
                call_protocol.claim(world.tenant_id, candidate.pk)
            except pgmei.GuideDeferredError:
                return "deferred"
            return "claimed"

        def settle_runner() -> str:
            worker.kind = "settlement"
            assert admitting.wait(timeout=10)
            return pgmei.settle_guide(world.tenant_id, predecessor.pk)

        with monkeypatch.context() as patcher:
            patcher.setattr(pgmei, "guide_admission", admission)
            patcher.setattr(pgmei, "lock_controls", controls)
            with ThreadPoolExecutor(max_workers=2) as pool:
                claim_future = pool.submit(runtime_thread, claim_runner)
                settle_future = pool.submit(runtime_thread, settle_runner)
                assert claim_future.result(timeout=15) == "deferred"
                assert settle_future.result(timeout=15) == "guia_nao_obtida"
        ticket = call_protocol.claim(world.tenant_id, candidate.pk)
        assert ticket is not None
        call_protocol.perform(ticket, adapter_for(world, prepared, peer.origin))
        pgmei.settle_guide(world.tenant_id, candidate.pk)
        assert len(peer.requests) == 2
        assert links(world) == 1
        with tenant_context(world.tenant_id):
            budget = ProviderBudget.objects.get()
            assert (budget.reserved, budget.known_liability) == (0, PRICE_MICROBRL)


def test_guide_client_lock_is_held_inside_the_real_claim(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = guide_world
    call, _ = reserve_on(world, monkeypatch, 0)
    real_lock = pgmei._lock_guide_client
    observed: list[bool] = []

    def contend(client_id: UUID) -> bool:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
            with tenant_context(world.tenant_id):
                ClientCompany.objects.select_for_update(nowait=True).get(pk=client_id)
        except DatabaseError:
            return True
        else:
            return False
        finally:
            connections.close_all()

    def locked(client_id: UUID) -> ClientCompany:
        client = real_lock(client_id)
        with ThreadPoolExecutor(max_workers=1) as pool:
            observed.append(pool.submit(contend, client_id).result(timeout=10))
        return client

    monkeypatch.setattr(pgmei, "_lock_guide_client", locked)
    assert call_protocol.claim(world.tenant_id, call.pk) is not None
    assert observed == [True]


@pytest.mark.parametrize("operation", ["SP-SIT-P", "SP-SIT-R", "SP-PAG", "SP-COMP"])
def test_other_operations_never_enter_the_guide_hook(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    world = guide_world
    call, _ = reserve_on(world, monkeypatch, 0)

    def forbidden(call: ProviderCall) -> bool:
        message = f"guide admission was called for {call.operation}"
        raise AssertionError(message)

    with tenant_context(world.tenant_id):
        call.operation = operation
        expected = registration.client_authority(call)
    monkeypatch.setattr(pgmei, "guide_admission", forbidden)
    with tenant_context(world.tenant_id):
        assert registration.client_authority(call) is expected


@pytest.mark.parametrize(
    "operation", ["SP-SIT-P", "SP-SIT-R", "SP-PAG", "SP-COMP", "SP-DAS"]
)
def test_client_authority_cannot_bypass_the_service_decision(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    world = guide_world
    call, _ = reserve_on(world, monkeypatch, 0)

    def forbidden(call: ProviderCall) -> bool:
        message = f"guide admission bypassed missing authority for {call.operation}"
        raise AssertionError(message)

    monkeypatch.setattr(
        registration,
        "authority_for",
        lambda *_args, **_kwargs: AuthorityDecision(Admission.MISSING, "sem_registro"),
    )
    monkeypatch.setattr(pgmei, "guide_admission", forbidden)
    with tenant_context(world.tenant_id):
        call.operation = operation
        assert registration.client_authority(call) is False
