"""AXRM-019 repair 4: a setup that lost its lock cannot reopen a stream disable.

RV4-LOST-LOCK-SETUP-REOPENS-STREAM-DISABLE (plan :1061, which covers --stream): a
stream-scoped disable leaves the link active, so the setup's post-write check must not
key on the disablement state alone. Every disable bumps a per-link epoch; a setup whose
epoch moved since its fence turns off every stream it wrote that the link no longer
holds, confirms that by read-back and refuses, and never merges the stream back in.
"""

import json
import os
import threading
from collections.abc import Callable

import psycopg
import pytest
from django.db import connection as db_connection
from django.db.backends.utils import CursorWrapper
from pytest_django.fixtures import SettingsWrapper

from apps.integrations.focus import company as focus_company
from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import ProviderRequest, ProviderResponse
from tests.integrations.test_focus_adapter import World, activate, disable, setup, world
from tests.integrations.test_focus_adapter_edges import outcome_of

__all__ = ["world"]
db = pytest.mark.django_db(transaction=True)
FLAGS = {
    "nfsen_recebidas": "habilita_nfsen_recebidas_homologacao",
    "nfe_recebidas": "habilita_manifestacao_homologacao",
}
QUALIFICATION = "AXRM-PQ-FOCUS-1-2026-11-05"


def _in_thread(action: Callable[[], str]) -> str:
    """Run a command to completion on its own connection, as another process would."""
    result: list[str] = []

    def run() -> None:
        try:
            result.append(outcome_of(action))
        finally:
            db_connection.close()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=60)
    assert not worker.is_alive()
    return result[0]


def _setup_racing(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    stream: str,
    disable_args: tuple[str, ...],
    streams: str = "",
) -> tuple[str, str]:
    """Inside the setup's enabling send: lose its lock, run a disable to completion."""
    pids: list[int] = []
    disabled: list[str] = []
    original_wait = focus_company._wait_for_link_lock
    original_send = ProviderTransport.send

    def wait(cursor: CursorWrapper, key: str) -> None:
        original_wait(cursor, key)
        cursor.execute("SELECT pg_backend_pid()")
        row = cursor.fetchone()
        pids.append(int(row[0]) if row else 0)

    def send(
        self: ProviderTransport, operation: str, request: ProviderRequest
    ) -> ProviderResponse:
        body = json.loads(request.body or b"{}")
        if body.get(FLAGS[stream]) is True and not disabled:
            with (
                psycopg.connect(
                    os.environ["SUPERUSER_DATABASE_URL"], autocommit=True
                ) as admin,
                admin.cursor() as cursor,
            ):
                cursor.execute("SELECT pg_terminate_backend(%s)", [pids[-1]])
            disabled.append(_in_thread(lambda: disable(world, *disable_args)))
        return original_send(self, operation, request)

    monkeypatch.setattr(focus_company, "_wait_for_link_lock", wait)
    monkeypatch.setattr(ProviderTransport, "send", send)
    outcome = outcome_of(
        lambda: setup(world, "--streams", streams or stream, "--start-month", "2026-11")
    )
    assert len(disabled) == 1
    return outcome, disabled[0]


@db
@pytest.mark.parametrize("stream", sorted(FLAGS))
def test_a_lost_lock_setup_cannot_reopen_a_stream_disable(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
    stream: str,
) -> None:
    # Given both streams active and a setup of one of them losing its lock mid-send.
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = QUALIFICATION
    activate(world, streams=sorted(FLAGS))
    world.fake.company[FLAGS["nfe_recebidas"]] = True
    other = next(name for name in FLAGS if name != stream)
    # When `focus_company_disable --stream <stream>` completes before the setup's PUT.
    outcome, disabled = _setup_racing(world, monkeypatch, stream, ("--stream", stream))
    # Then the disable holds: the setup undid its write, confirmed it, and refused.
    assert not disabled.startswith("CommandError"), disabled
    assert outcome.startswith("CommandError: Uma desativação ocorreu"), outcome
    assert "(confirmado)" in outcome
    assert world.fake.company[FLAGS[stream]] is False
    assert world.fake.company[FLAGS[other]] is True
    link = world.reload()
    assert list(link.streams) == [other]
    assert link.disablement_state == "ativa"


@db
def test_a_disable_of_another_stream_leaves_the_setup_alone(
    world: World, monkeypatch: pytest.MonkeyPatch, settings: SettingsWrapper
) -> None:
    # Given both streams active; the setup re-applies NFS-e while NF-e is disabled.
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = QUALIFICATION
    activate(world, streams=sorted(FLAGS))
    world.fake.company[FLAGS["nfe_recebidas"]] = True
    outcome, disabled = _setup_racing(
        world, monkeypatch, "nfsen_recebidas", ("--stream", "nfe_recebidas")
    )
    # Then nothing the setup wrote was removed: it confirms, and NF-e stays off.
    assert not disabled.startswith("CommandError"), disabled
    assert not outcome.startswith("CommandError"), outcome
    assert "Configuração confirmada" in outcome, outcome
    assert world.fake.company[FLAGS["nfsen_recebidas"]] is True
    assert world.fake.company[FLAGS["nfe_recebidas"]] is False
    assert list(world.reload().streams) == ["nfsen_recebidas"]


@db
def test_a_two_stream_setup_undoes_only_the_stream_the_disable_removed(
    world: World, monkeypatch: pytest.MonkeyPatch, settings: SettingsWrapper
) -> None:
    # Given a setup re-applying both streams while NFS-e alone is disabled.
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = QUALIFICATION
    activate(world, streams=sorted(FLAGS))
    world.fake.company[FLAGS["nfe_recebidas"]] = True
    outcome, disabled = _setup_racing(
        world,
        monkeypatch,
        "nfsen_recebidas",
        ("--stream", "nfsen_recebidas"),
        streams="nfe_recebidas,nfsen_recebidas",
    )
    # Then only the removed stream is switched off again; the kept one stays on.
    assert not disabled.startswith("CommandError"), disabled
    assert outcome.startswith("CommandError: Uma desativação ocorreu"), outcome
    assert "(confirmado)" in outcome
    assert world.fake.company[FLAGS["nfsen_recebidas"]] is False
    assert world.fake.company[FLAGS["nfe_recebidas"]] is True
    assert list(world.reload().streams) == ["nfe_recebidas"]
