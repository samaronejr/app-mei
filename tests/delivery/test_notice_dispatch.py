"""Real runtime locks, independent journal and SMTP at notice dispatch start."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import TypeVar

import pytest
from django.core.mail import EmailMessage
from django.db import OperationalError, connection

from apps.authz.approvals import Authority
from apps.core.tenancy import tenant_context
from apps.cycles.effects.manifest import JSONValue
from apps.delivery import gateway, journal, notice_authority
from apps.delivery.contracts import Reply
from apps.delivery.models import MessageState, OutboundMessage, TenantNoticePolicy
from apps.delivery.notice_authority import configure
from apps.delivery.services import claim_batch, finalize
from apps.tenants.models import Tenant
from tests.cycles.cycle_fixtures import CycleSetup
from tests.delivery import notice_fixtures
from tests.delivery.notice_fixtures import acknowledge, manual_notice, second_owner
from tests.delivery.smtp import FakeSMTP

pytestmark = pytest.mark.django_db(transaction=True)
graph = notice_fixtures.graph
T = TypeVar("T")


def _runtime[T](action: Callable[[], T], *, timeout: bool = False) -> T:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")
            if timeout:
                # Exclusion is the behavior: the holder cannot release until this
                # bounded PostgreSQL lock timeout has been observed. No polling.
                cursor.execute("SET lock_timeout = '250ms'")
        return action()
    finally:
        connection.close()


@pytest.mark.parametrize("owner_before_issuer", [True, False])
def test_revoke_commits_first_dispatch_refuses_without_journal_or_data(
    graph: CycleSetup, smtp_server: Callable[[str], FakeSMTP], owner_before_issuer: bool
) -> None:
    other = second_owner(graph)
    owner, issuer = (
        (graph.actor, other) if owner_before_issuer else (other, graph.actor)
    )
    acknowledge(graph, actor=owner)
    message = manual_notice(graph, actor=issuer)
    claim = claim_batch(graph.tenant.pk)[0]
    held, release = Event(), Event()

    def revoke() -> None:
        with tenant_context(graph.tenant.pk):
            configure(owner, kinds=(), acknowledged=False, expected_version=1)
            held.set()
            assert release.wait(15), "Revoke holder was not released"

    server = smtp_server("accept")
    with ThreadPoolExecutor(max_workers=2) as pool:
        revoker = pool.submit(_runtime, revoke)
        try:
            assert held.wait(15), "Revoke did not reach its real row fences"
            contender = pool.submit(_runtime, lambda: gateway.send(claim), timeout=True)
            with pytest.raises(OperationalError) as error:
                contender.result(timeout=15)
            assert getattr(error.value.__cause__, "sqlstate", None) == "55P03"
            assert server.received == []
            with tenant_context(graph.tenant.pk):
                stored = OutboundMessage.objects.get(pk=message.pk)
                assert stored.send_started_at is None
        finally:
            release.set()
        revoker.result(timeout=15)
        reply = gateway.send(claim)
        assert reply.state == MessageState.CANCELLED
        assert server.received == []
        assert journal.read(graph.tenant.pk) == []
        # prepare_claim already committed cancellation; late finalization cannot
        # make that terminal state a second transition.
        assert not finalize(claim, reply)
    with tenant_context(graph.tenant.pk):
        message.refresh_from_db()
        assert message.state == MessageState.CANCELLED
        assert message.send_started_at is None


@pytest.mark.parametrize("owner_before_issuer", [True, False])
def test_dispatch_start_commits_first_one_send_survives_later_revoke(
    graph: CycleSetup,
    smtp_server: Callable[[str], FakeSMTP],
    monkeypatch: pytest.MonkeyPatch,
    owner_before_issuer: bool,
) -> None:
    other = second_owner(graph)
    owner, issuer = (
        (graph.actor, other) if owner_before_issuer else (other, graph.actor)
    )
    acknowledge(graph, actor=owner)
    message = manual_notice(graph, actor=issuer)
    claim = claim_batch(graph.tenant.pk)[0]
    authority_held, release_authority, io_reached, release_io = (
        Event(),
        Event(),
        Event(),
        Event(),
    )
    original_basis, original_smtp = notice_authority._policy_basis, gateway.smtp_send

    def held_basis(
        policy: TenantNoticePolicy | None,
        authority: Authority | None,
        tenant: Tenant,
        kind: str,
        *,
        automatic: bool = False,
    ) -> dict[str, JSONValue]:
        basis = original_basis(policy, authority, tenant, kind, automatic=automatic)
        if not authority_held.is_set():
            authority_held.set()
            assert release_authority.wait(15), "Dispatch authority was not released"
        return basis

    def held_smtp(email: EmailMessage) -> Reply:
        assert not connection.in_atomic_block
        io_reached.set()
        assert release_io.wait(15), "SMTP barrier was not released"
        return original_smtp(email)

    def deliver() -> Reply:
        reply = gateway.send(claim)
        assert finalize(claim, reply)
        return reply

    def revoke() -> None:
        with tenant_context(graph.tenant.pk):
            configure(owner, kinds=(), acknowledged=False, expected_version=1)

    monkeypatch.setattr(notice_authority, "_policy_basis", held_basis)
    monkeypatch.setattr(gateway, "smtp_send", held_smtp)
    server = smtp_server("accept")
    with ThreadPoolExecutor(max_workers=2) as pool:
        dispatcher = pool.submit(_runtime, deliver)
        try:
            assert authority_held.wait(15)
            contender = pool.submit(_runtime, revoke, timeout=True)
            with pytest.raises(OperationalError) as error:
                contender.result(timeout=15)
            assert getattr(error.value.__cause__, "sqlstate", None) == "55P03"
            release_authority.set()
            assert io_reached.wait(15)
            with tenant_context(graph.tenant.pk):
                message.refresh_from_db()
                assert message.send_started_at is not None
                configure(owner, kinds=(), acknowledged=False, expected_version=1)
            assert [record.outcome for record in journal.read(graph.tenant.pk)] == [
                "intent_started"
            ]
            assert server.received == []
        finally:
            release_authority.set()
            release_io.set()
        assert dispatcher.result(timeout=15).state == MessageState.ACCEPTED
        assert len(server.received) == 1
    with tenant_context(graph.tenant.pk):
        message.refresh_from_db()
        assert (message.state, message.send_started_at is not None) == (
            MessageState.ACCEPTED,
            True,
        )
