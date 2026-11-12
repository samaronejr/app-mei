"""AXRM-019 repair 2 witnesses: the per-link exchange lock itself."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from django.db import DEFAULT_DB_ALIAS, connections
from django.db import connection as db_connection
from django.db.backends.utils import CursorWrapper

from apps.integrations.focus import company as focus_company
from apps.integrations.focus.company import (
    link_exchange_lock,
    plan_disable,
    plan_setup,
    run_disable,
    run_setup,
)
from tests.integrations.test_focus_adapter import World, activate, world
from tests.integrations.test_focus_adapter_edges import (
    NFSEN,
    _disable,
    _setup,
    outcome_of,
)

__all__ = ["world"]
db = pytest.mark.django_db(transaction=True)
BUSY = "FocusRefusedError: Outra operação Focus neste vínculo está em andamento"


@contextmanager
def _other() -> Iterator[CursorWrapper]:
    other = connections.create_connection(DEFAULT_DB_ALIAS)
    try:
        other.ensure_connection()
        other.set_autocommit(False)
        with other.cursor() as cursor:
            yield cursor
    finally:
        other.close()


def _key(link_id: UUID) -> str:
    return f"focus-link:{link_id}"


def _free(link_id: UUID) -> bool:
    with _other() as cursor:
        cursor.execute(
            "SELECT pg_try_advisory_xact_lock(hashtext(%s))", [_key(link_id)]
        )
        row = cursor.fetchone()
        return bool(row and row[0])


@db
def test_setup_and_disable_hold_the_link_lock_through_every_put(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    held: list[bool] = []
    real = world.fake.answer

    def answer(method: str, path: str, body: bytes) -> tuple[int, bytes]:
        if method == "PUT":
            held.append(not _free(world.link.pk))
        return real(method, path, body)

    monkeypatch.setattr(world.fake, "answer", answer)
    assert outcome_of(_setup(world, *NFSEN)) == "ok"
    assert outcome_of(_disable(world)) == "ok"
    assert held == [True, True]
    assert _free(world.link.pk)


@db
@pytest.mark.parametrize("operation", ["setup", "disable"])
def test_a_busy_link_refuses_after_a_bounded_wait_with_nothing_sent(
    world: World, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setattr(focus_company, "LINK_LOCK_TIMEOUT", "50ms")
    if operation == "setup":
        setup_plan = plan_setup(world.client.pk, "nfsen_recebidas", "2026-11")
        action = lambda: run_setup(setup_plan)  # noqa: E731
    else:
        disable_plan = plan_disable(world.client.pk)
        action = lambda: run_disable(disable_plan)  # noqa: E731
    before = len(world.fake.requests)
    results: list[str] = []

    def attempt() -> None:
        try:
            results.append(outcome_of(action))
        finally:
            db_connection.close()

    worker = threading.Thread(target=attempt, daemon=True)
    with _other() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", [_key(world.link.pk)]
        )
        worker.start()
        worker.join(timeout=20)
        bounded = not worker.is_alive()
    worker.join(timeout=20)
    assert bounded
    assert [r.startswith(BUSY) for r in results] == [True]
    assert len(world.fake.requests) == before
    assert world.reload().disablement_state == "ativa"


@db
def test_one_link_lock_at_a_time(world: World) -> None:
    with (
        link_exchange_lock(world.link.pk),
        pytest.raises(RuntimeError),
        link_exchange_lock(uuid4()),
    ):
        pass
    assert _free(world.link.pk)


@db
def test_a_stale_plan_is_refused_without_waiting_for_a_busy_link(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The read-only pre-check refuses before the lock; the busy refusal never shows.
    monkeypatch.setattr(focus_company, "LINK_LOCK_TIMEOUT", "50ms")
    stale = plan_setup(world.client.pk, "nfsen_recebidas", "2026-11")
    assert run_setup(
        plan_setup(world.client.pk, "nfsen_recebidas", "2026-12")
    ).confirmed
    before = len(world.fake.requests)
    with _other() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", [_key(world.link.pk)]
        )
        outcome = outcome_of(lambda: run_setup(stale))
    assert outcome.startswith("FocusRefusedError: O mês de início da captura já foi")
    assert len(world.fake.requests) == before


@db
def test_a_repeated_disable_of_a_confirmed_link_completes_cleanly(world: World) -> None:
    # G07/G09: no second transition and no short-circuit; one write, one read-back.
    activate(world, disablement_state="desativada_no_provedor_confirmada")
    assert outcome_of(_disable(world)) == "ok"
    assert [r.method for r in world.fake.requests] == ["PUT", "GET"]
    assert world.reload().disablement_state == "desativada_no_provedor_confirmada"
