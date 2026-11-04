"""AXRM-017 repair 2: retain review 2's ownership and extra-emission oracles."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from apps.core.tenancy import tenant_context
from apps.integrations import call_protocol
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.serpro import pgmei
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations.pgmei_support import (
    GUIDE_PDF,
    GuideWorld,
    das_reply,
    guide_world,
)
from tests.integrations.test_pgmei_recovery import (
    expire_lease,
    guide_calls,
    next_day,
    obtain,
    ownership,
    refresh,
    sweep,
)

if TYPE_CHECKING:
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from apps.integrations.call_contracts import Claim

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["guide_world"]


def report(marker: str, *facts: object) -> None:
    sys.stdout.write(f"{marker} {' '.join(str(value) for value in facts)}\n")
    sys.stdout.flush()


@pytest.mark.parametrize("cut", ["reserved"])
def test_worker_death_at_each_protocol_boundary(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch, cut: str
) -> None:
    world = guide_world

    def killed_claim(tenant_id: UUID, call_id: UUID) -> Claim | None:
        message = "death before claim"
        raise SystemExit(message)

    with FakeSerpro((das_reply(),)) as peer:
        with monkeypatch.context() as patcher:
            patcher.setattr(pgmei, "claim", killed_claim)
            with pytest.raises(SystemExit):
                obtain(world, peer.origin)
        [call] = guide_calls(world)
        expire_lease(world, call)
        sweep(world, days=8)
        call = refresh(world, call)
        with tenant_context(world.tenant_id):
            reservation = ProviderReservation.objects.get(call=call)
            held = ProviderBudget.objects.get().reserved
        report(
            "RV2_DEATH",
            cut,
            "state",
            call.state,
            "attempts",
            call.attempts,
            "sends",
            len(peer.requests),
            "owned",
            ownership(world, call),
            "reservation",
            reservation.state,
            "held",
            held,
        )
        assert peer.requests == []
    assert ownership(world, call) or held == 0, (
        "abandoned reservation is neither released nor owned"
    )


def test_previous_day_reserved_runner_blocks_a_new_paid_click(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = guide_world
    real_claim = pgmei.claim
    first_id: UUID | None = None
    nested: list[pgmei.GuideOutcome] = []
    sends_during_first_reservation: list[int] = []
    with FakeSerpro(
        (das_reply(), das_reply(pdf=GUIDE_PDF + b"% second runner\n"))
    ) as peer:

        def overlapping_claim(tenant_id: UUID, call_id: UUID) -> Claim | None:
            nonlocal first_id
            if first_id is None:
                first_id = call_id
                next_day(monkeypatch)
                nested.append(obtain(world, peer.origin))
                sends_during_first_reservation.append(len(peer.requests))
                report(
                    "RV2_RESERVED_OVERLAP_MID",
                    "new_click",
                    nested[0].code,
                    "sends",
                    len(peer.requests),
                )
            return real_claim(tenant_id, call_id)

        monkeypatch.setattr(pgmei, "claim", overlapping_claim)
        first = obtain(world, peer.origin)
        calls = guide_calls(world)
        report(
            "RV2_RESERVED_OVERLAP_END",
            "first",
            first.code,
            "second",
            nested[0].code,
            "calls",
            [(call.state, call.billed, call.attempts) for call in calls],
            "sends",
            len(peer.requests),
        )
        assert sends_during_first_reservation == [0], (
            "new click paid while earlier call was RESERVED"
        )
        assert len(peer.requests) == 1, (
            "overlapping consolidation-day runners bought two guides"
        )


@pytest.mark.parametrize("interruption", ["death", "pause"])
def test_interrupted_definitive_401_retry_ends_owned(
    guide_world: GuideWorld,
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
    interruption: str,
) -> None:
    world = guide_world
    real_retry_claim = call_protocol.claim

    def interrupted_claim(tenant_id: UUID, call_id: UUID) -> Claim | None:
        if interruption == "pause":
            settings.SERPRO_PAUSED = True
            return real_retry_claim(tenant_id, call_id)
        message = "death after first 401, before retry claim"
        raise SystemExit(message)

    with FakeSerpro((Reply(status=401), das_reply())) as peer:
        with monkeypatch.context() as patcher:
            patcher.setattr(call_protocol, "claim", interrupted_claim)
            error = pgmei.GuideRefused if interruption == "pause" else SystemExit
            with pytest.raises(error):
                obtain(world, peer.origin)
        settings.SERPRO_PAUSED = False
        [first] = guide_calls(world)
        expire_lease(world, first)
        sweep(world, days=8)
        first = refresh(world, first)
        owned_after_sweep = ownership(world, first)
        next_day(monkeypatch)
        again = obtain(world, peer.origin)
        report(
            "RV2_INTERRUPTED_401",
            interruption,
            "earlier_state",
            first.state,
            "attempts",
            first.attempts,
            "owned_after_sweep",
            owned_after_sweep,
            "new_click",
            again.code,
            "new_call",
            again.call_id != first.pk,
            "sends",
            len(peer.requests),
        )
    assert owned_after_sweep, (
        "sent 401 retry is skipped forever after its worker or admission fails"
    )
