"""AXRM-019 repair 5: an old compensation never undoes a newer confirmed setup.

RV5-OLD-COMPENSATION-DISABLES-NEW-CONFIRMED-SETUP (plan :1061/:1063): a setup that lost
its lock decides to compensate, but a newer setup may restore the stream before the
compensation runs. The compensation re-acquires the link's exchange lock and decides
from the link as stored now; with nothing left to undo it sends nothing, and when the
lock is not granted it sends nothing and owns the pending re-disable instead.
"""

import json
import os
import threading
from collections.abc import Callable

import psycopg
import pytest
from django.db import connection
from django.db.backends.utils import CursorWrapper
from pytest_django.fixtures import SettingsWrapper

from apps.audit.models import Event
from apps.core.tenancy import tenant_context
from apps.integrations.focus import company as focus_company
from apps.integrations.focus.company import SetupPlan
from apps.integrations.models import ClientProviderLink, IntegrationIssue
from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import ProviderRequest, ProviderResponse
from tests.integrations.test_focus_adapter import World, activate, disable, setup, world
from tests.integrations.test_focus_adapter_edges import outcome_of
from tests.integrations.test_focus_repair4 import FLAGS, QUALIFICATION, _in_thread

__all__ = ["world"]
db = pytest.mark.django_db(transaction=True)
BOTH = ",".join(sorted(FLAGS))


def _admin() -> psycopg.Connection:
    return psycopg.connect(
        os.environ["SUPERUSER_DATABASE_URL"],
        dbname=connection.settings_dict["NAME"],
        autocommit=True,
    )


def _lose_lock_then_disable(
    world: World, monkeypatch: pytest.MonkeyPatch, stream: str
) -> list[str]:
    """In this thread's enabling send: lose the lock, then run `--stream` to the end."""
    owner = threading.current_thread()
    pids: list[int] = []
    disabled: list[str] = []
    original_wait = focus_company._wait_for_link_lock
    original_send = ProviderTransport.send

    def wait(cursor: CursorWrapper, key: str) -> None:
        original_wait(cursor, key)
        if threading.current_thread() is owner:
            cursor.execute("SELECT pg_backend_pid()")
            row = cursor.fetchone()
            pids.append(int(row[0]) if row else 0)

    def send(
        self: ProviderTransport, operation: str, request: ProviderRequest
    ) -> ProviderResponse:
        body = json.loads(request.body or b"{}")
        mine = threading.current_thread() is owner
        if mine and body.get(FLAGS[stream]) is True and not disabled:
            with _admin() as admin, admin.cursor() as cursor:
                cursor.execute("SELECT pg_terminate_backend(%s)", [pids[-1]])
            disabled.append(_in_thread(lambda: disable(world, "--stream", stream)))
        return original_send(self, operation, request)

    monkeypatch.setattr(focus_company, "_wait_for_link_lock", wait)
    monkeypatch.setattr(ProviderTransport, "send", send)
    return disabled


def _before_undo(
    monkeypatch: pytest.MonkeyPatch, step: Callable[[], None]
) -> list[int]:
    """Run `step` when the old setup reaches its compensation; count its PUTs."""
    original = focus_company._undo_setup
    calls: list[int] = []

    def undo(plan: SetupPlan, link: ClientProviderLink, occurrence: str) -> None:
        calls.append(1)
        step()
        original(plan, link, occurrence)

    monkeypatch.setattr(focus_company, "_undo_setup", undo)
    return calls


def _puts(world: World) -> int:
    return sum(1 for r in world.fake.requests if r.method == "PUT")


@db
@pytest.mark.parametrize("stream", sorted(FLAGS))
def test_an_old_compensation_keeps_a_newer_confirmed_setup(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
    stream: str,
) -> None:
    # Given both streams active and an old setup whose lock is lost while a
    # `--stream` disable completes; a newer setup then restores that stream.
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = QUALIFICATION
    activate(world, streams=sorted(FLAGS))
    world.fake.company[FLAGS["nfe_recebidas"]] = True
    disabled = _lose_lock_then_disable(world, monkeypatch, stream)
    newer: list[str] = []
    marks: list[int] = []

    def newer_setup() -> None:
        newer.append(
            _in_thread(
                lambda: setup(world, "--streams", stream, "--start-month", "2026-11")
            )
        )
        marks.append(_puts(world))

    calls = _before_undo(monkeypatch, newer_setup)
    # When the old setup reaches its compensation.
    outcome = outcome_of(
        lambda: setup(world, "--streams", BOTH, "--start-month", "2026-11")
    )
    # Then it refuses with nothing to undo and sends nothing; the newer setup holds.
    assert calls == [1]
    assert not disabled[0].startswith("CommandError"), disabled
    assert "Configuração confirmada" in newer[0], newer
    assert outcome.startswith("CommandError: Uma desativação ocorreu"), outcome
    assert "nada a desfazer" in outcome, outcome
    assert _puts(world) == marks[0]
    assert world.fake.company[FLAGS[stream]] is True
    assert sorted(world.reload().streams) == sorted(FLAGS)


@db
@pytest.mark.parametrize("stream", sorted(FLAGS))
def test_a_compensation_without_the_lock_sends_nothing_and_owns_the_redisable(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
    stream: str,
) -> None:
    # Given an old setup whose compensation finds the link's key held elsewhere.
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = QUALIFICATION
    activate(world, streams=sorted(FLAGS))
    world.fake.company[FLAGS["nfe_recebidas"]] = True
    disabled = _lose_lock_then_disable(world, monkeypatch, stream)
    monkeypatch.setattr(focus_company, "LINK_LOCK_TIMEOUT", "50ms")
    holder = _admin()
    marks: list[int] = []

    def hold_key() -> None:
        with holder.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_lock(hashtext(%s))", [f"focus-link:{world.link.pk}"]
            )
        marks.append(_puts(world))

    _before_undo(monkeypatch, hold_key)
    try:
        # When the old setup reaches its compensation.
        outcome = outcome_of(
            lambda: setup(world, "--streams", stream, "--start-month", "2026-11")
        )
    finally:
        holder.close()
    # Then nothing is sent; an issue and an event own the pending re-disable.
    assert not disabled[0].startswith("CommandError"), disabled
    assert outcome.startswith("CommandError: Uma desativação ocorreu"), outcome
    assert "nada foi enviado" in outcome, outcome
    assert _puts(world) == marks[0]
    with tenant_context(world.tenant.pk):
        reasons = list(IntegrationIssue.objects.values_list("reason", flat=True))
        deferred = Event.objects.filter(
            action="integrations.focus_setup_compensation_deferred"
        ).count()
    assert reasons == ["recebimento_reaberto_provedor"]
    assert deferred == 1


@db
@pytest.mark.parametrize("stream", sorted(FLAGS))
def test_a_confirmation_never_re_adds_a_stream_disabled_after_the_post_write_read(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
    stream: str,
) -> None:
    # Given a setup past its post-write read whose lock is lost while a
    # `--stream` disable of its own stream completes, before it confirms.
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = QUALIFICATION
    activate(world, streams=sorted(FLAGS))
    world.fake.company[FLAGS["nfe_recebidas"]] = True
    owner = threading.current_thread()
    pids: list[int] = []
    disabled: list[str] = []
    original_wait = focus_company._wait_for_link_lock
    original_confirm = focus_company._confirm_setup

    def wait(cursor: CursorWrapper, key: str) -> None:
        original_wait(cursor, key)
        if threading.current_thread() is owner:
            cursor.execute("SELECT pg_backend_pid()")
            row = cursor.fetchone()
            pids.append(int(row[0]) if row else 0)

    def confirm(plan: SetupPlan) -> ClientProviderLink:
        if not disabled:
            with _admin() as admin, admin.cursor() as cursor:
                cursor.execute("SELECT pg_terminate_backend(%s)", [pids[-1]])
            disabled.append(_in_thread(lambda: disable(world, "--stream", stream)))
        return original_confirm(plan)

    monkeypatch.setattr(focus_company, "_wait_for_link_lock", wait)
    monkeypatch.setattr(focus_company, "_confirm_setup", confirm)
    # When the setup confirms.
    outcome = outcome_of(
        lambda: setup(world, "--streams", stream, "--start-month", "2026-11")
    )
    # Then it does not restore the stream; the disable holds and the withheld
    # stream is on record.
    assert not disabled[0].startswith("CommandError"), disabled
    assert not outcome.startswith("CommandError"), outcome
    assert stream not in world.reload().streams
    assert world.fake.company[FLAGS[stream]] is False
    with tenant_context(world.tenant.pk):
        withheld = [
            meta["streams"]
            for meta in Event.objects.filter(
                action="integrations.focus_setup_streams_withheld"
            ).values_list("metadata", flat=True)
        ]
    assert withheld == [[stream]]


@db
def test_a_compensation_whose_new_lock_is_lost_before_the_write_sends_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch, settings: SettingsWrapper
) -> None:
    # Given an old setup that re-acquires the lock to compensate and then loses
    # that new lock too, right after its fresh re-read.
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = QUALIFICATION
    activate(world, streams=sorted(FLAGS))
    world.fake.company[FLAGS["nfe_recebidas"]] = True
    owner = threading.current_thread()
    pids: list[int] = []
    disabled = _lose_lock_then_disable(world, monkeypatch, "nfsen_recebidas")
    wait_after_disable = focus_company._wait_for_link_lock

    def wait(cursor: CursorWrapper, key: str) -> None:
        wait_after_disable(cursor, key)
        if threading.current_thread() is owner:
            cursor.execute("SELECT pg_backend_pid()")
            row = cursor.fetchone()
            pids.append(int(row[0]) if row else 0)

    original_reread = focus_company._reread_locked
    reads: list[int] = []

    def reread(plan: SetupPlan) -> ClientProviderLink:
        link = original_reread(plan)
        reads.append(1)
        if len(reads) == 2:  # the compensation's fresh read, under the new lock
            with _admin() as admin, admin.cursor() as cursor:
                cursor.execute("SELECT pg_terminate_backend(%s)", [pids[-1]])
            reads.append(_puts(world))
        return link

    monkeypatch.setattr(focus_company, "_wait_for_link_lock", wait)
    monkeypatch.setattr(focus_company, "_reread_locked", reread)
    # When the compensation reaches its write.
    outcome = outcome_of(
        lambda: setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    )
    # Then nothing is sent; the pending re-disable is owned instead.
    assert not disabled[0].startswith("CommandError"), disabled
    assert "o bloqueio do vínculo foi perdido" in outcome, outcome
    assert "nada foi enviado" in outcome, outcome
    assert _puts(world) == reads[2]
    with tenant_context(world.tenant.pk):
        reasons = [
            meta["reason"]
            for meta in Event.objects.filter(
                action="integrations.focus_setup_compensation_deferred"
            ).values_list("metadata", flat=True)
        ]
        issues = IntegrationIssue.objects.count()
    assert reasons == ["lock_lost_before_write"]
    assert issues == 1
