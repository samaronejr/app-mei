"""Retirement is not revocation; migration and restore never invent authority."""

from collections.abc import Callable

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleEvent
from apps.cycles.services.cycle import migrate_open_cycle
from apps.cycles.services.playbooks import (
    create_draft_version,
    publish_version,
    retire_version,
)
from apps.delivery import gateway, journal
from apps.delivery.automations import retry_dead_letter
from apps.delivery.models import MessageState, OutboundMessage, TenantNoticePolicy
from apps.delivery.notice_authority import validate_notice
from apps.delivery.services import claim_batch, finalize
from tests.cycles.cycle_fixtures import CycleSetup
from tests.delivery import notice_fixtures
from tests.delivery.conftest import DeliverySetup
from tests.delivery.notice_fixtures import acknowledge, manual_notice
from tests.delivery.smtp import FakeSMTP

pytestmark = pytest.mark.django_db(transaction=True)
graph = notice_fixtures.graph


def _restore_older_notice_schema(message: OutboundMessage) -> None:
    """Actual schema roundtrip loses bytes; forward migration must leave them null."""
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        MigrationExecutor(connection).migrate([("delivery", "0005_outbound_retry")])
        MigrationExecutor(connection).migrate(leaves)
    finally:
        MigrationExecutor(connection).migrate(leaves)
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")
    with tenant_context(message.tenant_id):
        message.refresh_from_db()
        assert message.notice_authorization_bytes is None
        assert message.notice_authorization_digest is None


def test_legacy_ambiguous_opening_cannot_borrow_a_current_publication(
    delivery: DeliverySetup,
) -> None:
    message = delivery.queue()
    _restore_older_notice_schema(message)
    with tenant_context(delivery.graph.tenant.pk):
        opening = CycleEvent.objects.get(cycle=delivery.cycle, type="cycle.opened")
        CycleEvent.objects.create(
            tenant=delivery.graph.tenant,
            client=delivery.graph.client,
            cycle=delivery.cycle,
            type=opening.type,
            actor=opening.actor,
            aggregate_type=opening.aggregate_type,
            aggregate_id=opening.aggregate_id,
            aggregate_version=opening.aggregate_version,
            payload=opening.payload,
            occurred_at=opening.occurred_at,
        )
        assert validate_notice(message) == (False, "notice_policy_missing")
        assert message.notice_authorization_bytes is None
        assert message.notice_authorization_digest is None


def test_retired_published_pin_remains_independent_authority(
    delivery: DeliverySetup,
) -> None:
    message = delivery.queue()
    assert message.notice_authorization_bytes is not None
    raw = bytes(message.notice_authorization_bytes)
    definition = delivery.graph.definition
    retire_version(
        actor=None,
        tenant=delivery.graph.tenant,
        playbook_pk=definition.playbook_id,
        number=definition.number,
        expected_version=definition.version,
    )
    with tenant_context(delivery.graph.tenant.pk):
        assert not TenantNoticePolicy.objects.exists()
        assert validate_notice(message) == (True, "authorized")
        assert message.notice_authorization_bytes is not None
        assert bytes(message.notice_authorization_bytes) == raw


def test_explicit_cycle_migration_invalidates_published_intent(
    delivery: DeliverySetup,
) -> None:
    message = delivery.queue()
    graph = delivery.graph
    graph.tenant.features = ["cycles_r1", "bulk_r1"]
    graph.tenant.save(update_fields=["features"])
    draft = create_draft_version(
        actor=None,
        tenant=graph.tenant,
        playbook_pk=graph.definition.playbook_id,
    )
    target, _ = publish_version(
        actor=None,
        tenant=graph.tenant,
        playbook_pk=draft.playbook_id,
        number=draft.number,
        expected_version=draft.version,
    )
    with tenant_context(graph.tenant.pk):
        migrate_open_cycle(
            graph.actor,
            delivery.cycle.pk,
            target.pk,
            expected_version=delivery.cycle.version,
        )
        assert validate_notice(message) == (False, "notice_playbook_changed")


def test_legacy_provable_publication_is_read_only_and_needs_no_policy(
    delivery: DeliverySetup,
    smtp_server: Callable[[str], FakeSMTP],
) -> None:
    message = delivery.queue()
    _restore_older_notice_schema(message)
    with tenant_context(delivery.graph.tenant.pk):
        assert not TenantNoticePolicy.objects.exists()
        assert validate_notice(message) == (True, "legacy_published")
        message.refresh_from_db()
        assert message.notice_authorization_bytes is None
    server = smtp_server("accept")
    claim = claim_batch(delivery.graph.tenant.pk)[0]
    reply = gateway.send(claim)
    assert reply.state == MessageState.ACCEPTED
    assert finalize(claim, reply)
    assert len(server.received) == 1
    with tenant_context(delivery.graph.tenant.pk):
        message.refresh_from_db()
        assert message.notice_authorization_bytes is None
        assert not TenantNoticePolicy.objects.exists()


def test_restored_standalone_intent_cannot_borrow_current_acknowledgement(
    graph: CycleSetup,
) -> None:
    acknowledge(graph)
    message = manual_notice(graph)
    _restore_older_notice_schema(message)
    acknowledge(graph)
    with tenant_context(graph.tenant.pk):
        assert validate_notice(message) == (False, "notice_policy_missing")
        assert message.notice_authorization_bytes is None


def test_explicit_retry_copies_original_binding_then_revoke_blocks_successor(
    graph: CycleSetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    acknowledge(graph)
    parent = manual_notice(graph)
    server = smtp_server("reject")
    claim = claim_batch(graph.tenant.pk)[0]
    reply = gateway.send(claim)
    assert reply.state == MessageState.REJECTED
    assert finalize(claim, reply)
    with tenant_context(graph.tenant.pk):
        parent.refresh_from_db()
        successor = retry_dead_letter(
            graph.actor, parent.pk, expected_version=parent.version
        )
        assert successor.retry_of_id == parent.pk
        assert successor.notice_authorization_bytes == parent.notice_authorization_bytes
        assert (
            successor.notice_authorization_digest == parent.notice_authorization_digest
        )
    acknowledge(graph)
    successor_claim = claim_batch(graph.tenant.pk)[0]
    refused = gateway.send(successor_claim)
    assert refused.state == MessageState.CANCELLED
    assert len(server.received) == 1
    assert len(journal.read(graph.tenant.pk)) == 2
