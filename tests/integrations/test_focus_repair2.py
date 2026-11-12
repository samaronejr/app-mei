"""AXRM-019 repair 2: a confirmed disable stays disabled at Focus.

RV2-STALE-SETUP-REOPENS-CONFIRMED-DISABLE: a setup planned while the link was active
must not re-enable capture once a disable has been confirmed (checked under the link's
row lock right before the write), and a repeated disable decides from Focus's
read-back, not from the local "already confirmed" state: it re-sends and confirms off.
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING

import pytest
from django.db import connection as db_connection

from apps.integrations.focus import company as focus_company
from apps.integrations.focus.company import FocusRefusedError, plan_setup, run_setup
from tests.integrations.test_focus_adapter import World, _capture, disable, world
from tests.integrations.test_focus_adapter_edges import (
    NFSEN,
    _disable,
    _setup,
    outcome_of,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from apps.integrations.transport import ProviderTransport
    from apps.integrations.transport_types import (
        Environment,
        ProviderRequest,
        ProviderResponse,
    )

__all__ = ["world"]
db = pytest.mark.django_db(transaction=True)
FLAG = "habilita_nfsen_recebidas_homologacao"
CONFIRMED = "desativada_no_provedor_confirmada"


def _puts(world: World) -> int:
    return sum(1 for r in world.fake.requests if r.method == "PUT")


@db
def test_a_setup_planned_before_a_confirmed_disable_is_refused_unsent(
    world: World,
) -> None:
    # Given a configured link, a setup planned while it was active, then a disable.
    assert outcome_of(_setup(world, *NFSEN)) == "ok"
    stale = plan_setup(world.client.pk, "nfsen_recebidas", "2026-11")
    disable(world)
    assert world.reload().disablement_state == CONFIRMED
    assert world.fake.company[FLAG] is False
    before = len(world.fake.requests)
    # When the stale plan runs (as a real setup and as a dry run).
    for dry_run in (False, True):
        with pytest.raises(FocusRefusedError, match=CONFIRMED):
            run_setup(stale, dry_run=dry_run)
    # Then nothing reached Focus and capture stays off on both sides.
    assert len(world.fake.requests) == before
    assert world.fake.company[FLAG] is False
    assert world.reload().disablement_state == CONFIRMED
    assert _capture(world) == []


@db
def test_a_repeated_disable_resends_and_confirms_false_from_the_read_back(
    world: World,
) -> None:
    # Given a confirmed disable whose provider switch is back on (a late write).
    assert outcome_of(_setup(world, *NFSEN)) == "ok"
    disable(world)
    assert world.reload().disablement_state == CONFIRMED
    world.fake.company[FLAG] = True
    before = _puts(world)
    # When the operator disables again.
    output = disable(world)
    # Then the switch is written off again and confirmed by read-back.
    assert _puts(world) == before + 1
    assert world.fake.company[FLAG] is False
    assert "Nada a fazer" not in output
    assert world.reload().disablement_state == CONFIRMED
    assert _capture(world) == []


class _PausedTransport:
    """Hold a setup's enabling PUT inside its send until the disable has moved."""

    def __init__(
        self, inner: ProviderTransport, paused: threading.Event, go: threading.Event
    ) -> None:
        self._inner = inner
        self._paused = paused
        self._go = go
        self.released: list[bool] = []

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def send(self, operation: str, request: ProviderRequest) -> ProviderResponse:
        if json.loads(request.body or b"{}").get(FLAG) is True:
            self._paused.set()
            self.released.append(self._go.wait(timeout=30))
        return self._inner.send(operation, request)


@db
def test_a_disable_racing_a_paused_setup_leaves_capture_off(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a setup that passed its fence and is paused inside its HTTP send.
    paused, go = threading.Event(), threading.Event()
    real_transport = focus_company._transport
    wrappers: list[_PausedTransport] = []

    def transport(environment: Environment) -> _PausedTransport:
        wrapper = _PausedTransport(real_transport(environment), paused, go)
        wrappers.append(wrapper)
        return wrapper

    monkeypatch.setattr(focus_company, "_transport", transport)
    real_wait = getattr(focus_company, "_wait_for_link_lock", None)
    if real_wait is not None:
        # The setup resumes once the disable is queued on the link lock.
        def waiting(*args: object) -> None:
            if threading.current_thread().name == "disable":
                go.set()
            real_wait(*args)

        monkeypatch.setattr(focus_company, "_wait_for_link_lock", waiting)
    outcomes: dict[str, str] = {}

    def run(name: str, action: Callable[[], None]) -> None:
        try:
            outcomes[name] = outcome_of(action)
        finally:
            if name == "disable":
                go.set()  # without a lock the setup resumes after the whole disable
            db_connection.close()

    setup = threading.Thread(
        target=run, args=("setup", _setup(world, *NFSEN)), name="setup", daemon=True
    )
    setup.start()
    assert paused.wait(timeout=30)
    # When a disable for the same link starts meanwhile.
    stopper = threading.Thread(
        target=run, args=("disable", _disable(world)), name="disable", daemon=True
    )
    stopper.start()
    for thread in (setup, stopper):
        thread.join(timeout=60)
    assert not [thread for thread in (setup, stopper) if thread.is_alive()]
    # Then whichever order the writes took, Focus ends with capture off, confirmed.
    assert outcomes["disable"] == "ok"
    assert world.fake.company[FLAG] is False
    assert world.reload().disablement_state == CONFIRMED
    assert _capture(world) == []
