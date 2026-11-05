"""Internal authentication retries receive their own final wire admission."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from apps.integrations import call_dispatch, call_protocol
from tests.fakes.provider import Reply
from tests.integrations.sitfis_support import (
    World,
    clock,
    envelope,
    protocol_reply,
    report_reply,
    sitfis,
    world,
)

if TYPE_CHECKING:
    from apps.integrations.call_contracts import CallResult
    from apps.integrations.call_dispatch import Adapter, Dispatch

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "world"]


def test_an_internal_401_retry_rechecks_a_reply_committed_after_its_start(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = sitfis()
    world.script(protocol_reply(30_000))
    report = world.advance(world.request())
    world.clock.advance(seconds=30)
    world.script(Reply(status=401, body=envelope(401, "")), report_reply())
    seen: list[int] = []

    def before_wire(dispatch: Dispatch, adapter: Adapter) -> CallResult:
        seen.append(dispatch.receipt.attempt_number)
        if dispatch.receipt.attempt_number == 2:
            mod._receive(
                world.tenant_id,
                report.pk,
                dispatch.claim.call_id,
                mod.SitfisReply(
                    received_at=world.clock.now(), wait=timedelta(seconds=300)
                ),
            )
        return call_dispatch.transmit(dispatch, adapter)

    monkeypatch.setattr(call_protocol, "transmit", before_wire)
    final = world.advance(report)
    assert seen == [1, 2]
    assert len(world.sent("/Emitir")) == 1
    assert final.state == "aguardando"
    [call] = world.calls("SP-SIT-R")
    assert call.attempts == 1
    budget = world.budget()
    assert (budget.reserved, budget.known_liability, budget.uncertain_exposure) == (
        0,
        0,
        0,
    )
