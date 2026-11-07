"""Walk every non-final guide state through abandonment, recovery and a later click."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from django.db import DatabaseError, connection, connections
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.integrations import call_protocol
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.budget_settlement import release as release_reservation
from apps.integrations.call_models import CallState, FirmProviderCall, ProviderCall
from apps.integrations.serpro import pgmei
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations.pgmei_support import (
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
    ownership,
    refresh,
    sweep,
)

if TYPE_CHECKING:
    from uuid import UUID

    from apps.integrations.call_contracts import Claim

__all__ = ["guide_world"]


def test_every_client_and_firm_protocol_state_has_one_classification() -> None:
    for model in (ProviderCall, FirmProviderCall):
        choices = model._meta.get_field("state").choices
        assert choices is not None
        assert set(pgmei.CALL_FINAL) == {value for value, _label in choices}
    assert set(pgmei.CALL_FINAL) == set(CallState.values)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "state", [state for state, final in pgmei.CALL_FINAL.items() if not final]
)
def test_abandoned_state_walk_releases_links_or_owns_without_a_second_bill(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    world = guide_world

    def die(*args: object, **kwargs: object) -> None:
        message = "death at the selected protocol boundary"
        raise SystemExit(message)

    # STARTED is the real dispatch-start commit before the socket opens. WAITING
    # is not produced by today's PDF adapter, but the protocol admits it, so walk
    # that durable state too. No send may be inferred safe from either state.
    with FakeSerpro(
        (Reply(status=401), das_reply()) if state == CallState.RETRY else (das_reply(),)
    ) as peer:
        with monkeypatch.context() as patcher:
            if state == CallState.RETRY:
                patcher.setattr(call_protocol, "claim", die)
            elif state == CallState.CLAIMED:
                patcher.setattr(pgmei, "perform", die)
            elif state in {CallState.STARTED, CallState.WAITING}:
                patcher.setattr(pgmei.GuideAdapter, "send", die)
            else:
                patcher.setattr(pgmei, "claim", die)
            with pytest.raises(SystemExit):
                obtain(world, peer.origin)
        [call] = guide_calls(world)
        if state in {CallState.QUEUED, CallState.WAITING}:
            with tenant_context(world.tenant_id):
                ProviderCall.objects.filter(pk=call.pk).update(state=state)
        assert refresh(world, call).state == state
        # A day change alone does not permit another emission in any non-final
        # state, including the two states the shared PDF adapter does not expose.
        next_day(monkeypatch)
        waiting = obtain(world, peer.origin)
        assert waiting.code == "em_andamento"
        assert waiting.call_id == call.pk
        assert len(peer.requests) == (1 if state == CallState.RETRY else 0)
        expire_lease(world, call)
        sweep(world, days=8)
        call = refresh(world, call)
        linked = links(world) == 1
        owned = bool(ownership(world, call)) and not linked
        released = call.state == CallState.CANCELLED
        assert sum((released, linked, owned)) == 1
        assert pgmei.CALL_FINAL[call.state]
        with tenant_context(world.tenant_id):
            reservation = ProviderReservation.objects.get(call=call)
            budget = ProviderBudget.objects.get()
            assert budget.reserved == 0
            assert reservation.state == "released" or (
                owned
                and reservation.state == "uncertain"
                and budget.uncertain_exposure == PRICE_MICROBRL
            )
        # Settlement is idempotent, and a delayed original task cannot buy
        # anything. The next deliberate local-day click may buy one guide after
        # release/ownership; the complete 401 remains definitely unbilled.
        pgmei.settle_guide(world.tenant_id, call.pk)
        later = obtain(world, peer.origin)
        assert later.code == "guia_obtida"
        assert len(peer.requests) - (1 if state == CallState.RETRY else 0) == 1
        assert refresh(world, call).state == call.state


@pytest.mark.django_db(transaction=True)
def test_reservation_release_holds_the_call_lock_and_advances_its_fence(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = guide_world

    def die(*args: object, **kwargs: object) -> None:
        raise SystemExit

    with FakeSerpro((das_reply(),)) as peer:
        with monkeypatch.context() as patcher:
            patcher.setattr(pgmei, "claim", die)
            with pytest.raises(SystemExit):
                obtain(world, peer.origin)
        [call] = guide_calls(world)
        before_fence = call.fence
        expire_lease(world, call)
        checked: list[bool] = []

        def competing_claim_lock() -> bool:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET ROLE app_runtime")
                with tenant_context(world.tenant_id):
                    ProviderCall.objects.select_for_update(nowait=True).get(pk=call.pk)
            except DatabaseError:
                return True
            else:
                return False
            finally:
                connections.close_all()

        def fenced_release(locked: ProviderCall) -> None:
            assert connection.in_atomic_block
            with ThreadPoolExecutor(max_workers=1) as pool:
                held = pool.submit(competing_claim_lock).result(timeout=10)
            checked.append(held)
            assert held, "another runner acquired the call during reservation release"
            release_reservation(locked)

        monkeypatch.setattr(pgmei, "release", fenced_release)
        sweep(world)
        after = refresh(world, call)
        assert checked == [True]
        assert after.fence > before_fence
        assert after.state == CallState.CANCELLED
        assert peer.requests == []


@pytest.mark.django_db(transaction=True)
def test_delayed_reserved_runner_cannot_send_after_another_runner_releases_it(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = guide_world
    real_claim = call_protocol.claim

    def delayed_claim(tenant_id: UUID, call_id: UUID) -> Claim | None:
        [call] = guide_calls(world)
        expire_lease(world, call)
        sweep(world)
        return real_claim(tenant_id, call_id)

    with FakeSerpro((das_reply(),)) as peer:
        with monkeypatch.context() as patcher:
            patcher.setattr(pgmei, "claim", delayed_claim)
            with pytest.raises(pgmei.GuideRefused) as refused:
                obtain(world, peer.origin)
        assert refused.value.code == "cancelada"
        assert peer.requests == []
        [call] = guide_calls(world)
        assert call.state == CallState.CANCELLED
        assert call.attempts == 0
        with tenant_context(world.tenant_id):
            assert ProviderBudget.objects.get().reserved == 0
        next_day(monkeypatch)
        assert obtain(world, peer.origin).code == "guia_obtida"
        assert len(peer.requests) == 1


@pytest.mark.django_db(transaction=True)
def test_unclaimed_handoff_expires_without_ever_creating_a_claim_lease(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = guide_world

    def die(*args: object, **kwargs: object) -> None:
        raise SystemExit

    with FakeSerpro((das_reply(),)) as peer:
        with monkeypatch.context() as patcher:
            patcher.setattr(pgmei, "claim", die)
            with pytest.raises(SystemExit):
                obtain(world, peer.origin)
        [call] = guide_calls(world)
        assert call.lease_until is None
        sweep(world)
        assert refresh(world, call).state == CallState.RESERVED
        clock = timezone.now()
        monkeypatch.setattr(timezone, "now", lambda: clock + timedelta(seconds=121))
        sweep(world)
        assert refresh(world, call).state == CallState.CANCELLED
        with tenant_context(world.tenant_id):
            assert ProviderReservation.objects.get(call=call).state == "released"
            assert ProviderBudget.objects.get().reserved == 0
        assert peer.requests == []
