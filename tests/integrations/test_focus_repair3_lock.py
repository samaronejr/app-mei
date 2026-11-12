"""AXRM-019 repair 3: a lost link lock can never reopen a confirmed disable.

RV3-LOCK-CONNECTION-LOSS-REOPENS-CONFIRMED-DISABLE: the advisory lock lives on its own
connection and can be lost. Right before its write, setup proves it still holds the
lock (else nothing is sent); after its write it re-reads the link, and if a disable
started or finished meanwhile it switches receipt off again and reports a refusal.
"""

import json
import os

import psycopg
import pytest
from django.db.backends.utils import CursorWrapper

from apps.integrations.focus import company as focus_company
from apps.integrations.focus.company import SetupPlan
from apps.integrations.models import ClientProviderLink
from apps.integrations.services import LinkMachine, transition_link
from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import ProviderRequest, ProviderResponse
from tests.integrations.test_focus_adapter import World, _capture, world
from tests.integrations.test_focus_adapter_edges import NFSEN, _setup, outcome_of

__all__ = ["world"]
db = pytest.mark.django_db(transaction=True)
FLAG = "habilita_nfsen_recebidas_homologacao"
NFE_FLAG = "habilita_manifestacao_homologacao"
CONFIRMED = "desativada_no_provedor_confirmada"


def _enables(request: ProviderRequest) -> bool:
    return json.loads(request.body or b"{}").get(FLAG) is True


@db
def test_a_disable_finished_during_the_setup_write_is_undone_and_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a setup whose enabling write is in flight when a disable completes
    # (as when the setup's lock connection was lost mid-exchange).
    original = ProviderTransport.send
    disabled: list[str] = []

    def send(
        self: ProviderTransport, operation: str, request: ProviderRequest
    ) -> ProviderResponse:
        if _enables(request) and not disabled:
            link = world.reload()
            for target in (*focus_company.DISABLE_PATH[1:], CONFIRMED):
                link = transition_link(
                    world.tenant.pk,
                    link.pk,
                    machine=LinkMachine.DISABLEMENT,
                    to=target,
                    expected_version=link.version,
                )
            world.fake.company[FLAG] = False
            # A switch this setup never wrote is on too: the undo turns it off as well.
            world.fake.company[NFE_FLAG] = True
            disabled.append(link.disablement_state)
        return original(self, operation, request)

    monkeypatch.setattr(ProviderTransport, "send", send)
    # When the setup's write lands after the disable.
    outcome = outcome_of(_setup(world, *NFSEN))
    # Then the setup switches receipt off again, confirms it, and is refused.
    assert disabled == [CONFIRMED]
    assert outcome.startswith("CommandError: Uma desativação ocorreu"), outcome
    assert "(confirmado)" in outcome
    assert world.fake.company[FLAG] is False
    assert world.fake.company[NFE_FLAG] is False
    assert world.reload().disablement_state == CONFIRMED
    assert _capture(world) == []


@db
def test_a_setup_that_lost_its_lock_before_the_write_sends_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a setup whose lock backend is terminated after the fence, before the PUT.
    pids: list[int] = []
    original_wait = focus_company._wait_for_link_lock
    original_fence = focus_company._fence_start_month

    def wait(cursor: CursorWrapper, key: str) -> None:
        original_wait(cursor, key)
        cursor.execute("SELECT pg_backend_pid()")
        row = cursor.fetchone()
        pids.append(int(row[0]) if row else 0)

    def fence(plan: SetupPlan, *, dry_run: bool) -> ClientProviderLink:
        link = original_fence(plan, dry_run=dry_run)
        if pids and len(pids) == 1:
            with (
                psycopg.connect(
                    os.environ["SUPERUSER_DATABASE_URL"], autocommit=True
                ) as admin,
                admin.cursor() as cursor,
            ):
                cursor.execute("SELECT pg_terminate_backend(%s)", [pids[0]])
            pids.append(-1)
        return link

    monkeypatch.setattr(focus_company, "_wait_for_link_lock", wait)
    monkeypatch.setattr(focus_company, "_fence_start_month", fence)
    # When the setup reaches its write.
    outcome = outcome_of(_setup(world, *NFSEN))
    # Then it is refused and nothing reached Focus.
    assert pids[1:] == [-1]
    assert outcome.startswith("CommandError: O bloqueio deste vínculo foi perdido")
    assert [r.method for r in world.fake.requests if r.method == "PUT"] == []


@db
def test_a_lock_released_and_taken_by_another_backend_refuses_unsent(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a setup whose lock connection stays alive but loses the key, which
    # another backend then holds (only the lock connection's own hold counts).
    original_wait = focus_company._wait_for_link_lock
    other = psycopg.connect(os.environ["SUPERUSER_DATABASE_URL"], autocommit=True)

    def wait(cursor: CursorWrapper, key: str) -> None:
        original_wait(cursor, key)
        cursor.execute("ROLLBACK")
        with other.cursor() as holder:
            holder.execute("SELECT pg_advisory_lock(hashtext(%s))", [key])

    monkeypatch.setattr(focus_company, "_wait_for_link_lock", wait)
    try:
        outcome = outcome_of(_setup(world, *NFSEN))
    finally:
        other.close()
    assert outcome.startswith("CommandError: O bloqueio deste vínculo foi perdido")
    assert [r.method for r in world.fake.requests if r.method == "PUT"] == []
