"""Current consent and owner lifetime, independent from stored authority bytes."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event

import pytest
from django.db import connection
from django.utils import timezone

from apps.authz.models import Capability, GrantLevel, RoleGrant, UserCapabilityGrant
from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.delivery import gateway, journal
from apps.delivery.contracts import Reply
from apps.delivery.errors import IntentConflict
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.services import claim_batch, finalize
from apps.tenants.models import Membership, TenantRole
from tests.cycles.approval_fixtures import LockSignal
from tests.cycles.cycle_fixtures import CycleSetup
from tests.delivery import notice_fixtures
from tests.delivery.notice_fixtures import acknowledge, manual_notice, second_owner
from tests.delivery.smtp import FakeSMTP
from tests.delivery.test_notice_dispatch import _runtime

pytestmark = pytest.mark.django_db(transaction=True)
graph = notice_fixtures.graph


@pytest.mark.parametrize(
    "change",
    [
        "owner_inactive",
        "owner_superuser",
        "owner_demoted",
        "owner_seat_stale",
        "policy_revision",
        "contact_permission",
        "contact_address",
    ],
)
def test_claim_does_not_freeze_live_notice_authority(
    graph: CycleSetup, smtp_server: Callable[[str], FakeSMTP], change: str
) -> None:
    owner = second_owner(graph)
    acknowledge(graph, actor=owner)
    message = manual_notice(graph)
    assert message.notice_authorization_bytes is not None
    raw = bytes(message.notice_authorization_bytes)
    claim = claim_batch(graph.tenant.pk)[0]
    if change == "owner_inactive":
        owner.is_active = False
        owner.save(update_fields=["is_active"])
    elif change == "owner_superuser":
        owner.is_superuser = True
        owner.save(update_fields=["is_superuser"])
    elif change in {"owner_demoted", "owner_seat_stale"}:
        seat = Membership.objects.get(user=owner, tenant=graph.tenant, client=None)
        if change == "owner_demoted":
            seat.role = TenantRole.STAFF_ACCOUNTANT
            seat.save(update_fields=["role"])
        else:
            seat.is_active = False
            seat.save(update_fields=["is_active"])
            seat.is_active = True
            seat.save(update_fields=["is_active"])
    elif change == "policy_revision":
        acknowledge(graph, actor=owner)
    else:
        with tenant_context(graph.tenant.pk):
            contact = ClientContact.objects.get(pk=message.contact_id)
            if change == "contact_permission":
                contact.channel_email_allowed = False
                contact.save(update_fields=["channel_email_allowed"])
            else:
                contact.email = "changed-recipient@example.test"
                contact.save(update_fields=["email"])
    server = smtp_server("accept")
    reply = gateway.send(claim)
    assert reply.state == MessageState.CANCELLED
    assert not finalize(claim, reply)
    assert server.received == []
    assert journal.read(graph.tenant.pk) == []
    with tenant_context(graph.tenant.pk):
        message.refresh_from_db()
        assert message.send_started_at is None
        assert message.notice_authorization_bytes is not None
        assert bytes(message.notice_authorization_bytes) == raw
        assert message.state == MessageState.CANCELLED


def test_duplicate_keeps_one_intent_and_cannot_rebind_after_reacknowledgement(
    graph: CycleSetup,
) -> None:
    acknowledge(graph)
    first = manual_notice(graph)
    duplicate = manual_notice(graph)
    assert first.pk == duplicate.pk
    assert first.notice_authorization_bytes == duplicate.notice_authorization_bytes
    acknowledge(graph)
    with pytest.raises(IntentConflict):
        manual_notice(graph)
    with tenant_context(graph.tenant.pk):
        assert OutboundMessage.objects.count() == 1


def test_owner_grant_expiry_after_last_blocking_source_refuses_dispatch(
    graph: CycleSetup,
    smtp_server: Callable[[str], FakeSMTP],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [timezone.now()]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    capability = Capability.objects.get(slug="automations.manage")
    role_grant = RoleGrant.objects.get(role=TenantRole.OWNER, capability=capability)
    original_level = role_grant.level
    role_grant.level = GrantLevel.NONE
    role_grant.save(update_fields=["level"])
    try:
        with tenant_context(graph.tenant.pk):
            grant = UserCapabilityGrant.objects.create(
                tenant=graph.tenant,
                user=graph.actor,
                capability=capability,
                level=GrantLevel.FULL,
                granted_by=graph.actor,
                expires_at=clock[0] + timedelta(minutes=1),
            )
        acknowledge(graph)
        message = manual_notice(graph)
        clock[0] = message.next_attempt_at
        with tenant_context(graph.tenant.pk):
            grant.expires_at = clock[0] + timedelta(seconds=1)
            grant.save(update_fields=["expires_at"])
        claim = claim_batch(graph.tenant.pk)[0]
        entering = Event()
        server = smtp_server("accept")

        def deliver() -> Reply:
            with connection.execute_wrapper(
                LockSignal("clients_clientcontact", entering)
            ):
                return gateway.send(claim)

        with ThreadPoolExecutor(max_workers=1) as pool:
            with tenant_context(graph.tenant.pk):
                ClientContact.objects.select_for_update().get(pk=message.contact_id)
                dispatcher = pool.submit(_runtime, deliver)
                assert entering.wait(15)
                clock[0] += timedelta(seconds=2)
            reply = dispatcher.result(timeout=15)
        assert reply.state == MessageState.CANCELLED
        assert reply.code == "notice_owner_changed"
        assert server.received == []
        assert journal.read(graph.tenant.pk) == []
        with tenant_context(graph.tenant.pk):
            message.refresh_from_db()
            assert message.send_started_at is None
    finally:
        role_grant.level = original_level
        role_grant.save(update_fields=["level"])


def test_two_runtime_producers_commit_one_unchanged_intent(graph: CycleSetup) -> None:
    acknowledge(graph)
    first = manual_notice(graph)
    start = Barrier(2, timeout=15)

    def enqueue_duplicate() -> OutboundMessage:
        start.wait()
        return manual_notice(graph)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_runtime, enqueue_duplicate) for _ in range(2)]
        duplicates = [future.result(timeout=15) for future in futures]
    assert all(message.pk == first.pk for message in duplicates)
    assert all(
        message.notice_authorization_bytes == first.notice_authorization_bytes
        for message in duplicates
    )
    with tenant_context(graph.tenant.pk):
        assert OutboundMessage.objects.count() == 1
