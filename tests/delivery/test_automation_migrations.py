"""A populated rollback/reapply must preserve retry lineage and immutable history."""

from collections.abc import Callable

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.audit.models import Event
from apps.core.tenancy import tenant_context
from apps.delivery.automations import retry_dead_letter
from apps.delivery.models import DeliveryAttempt, DeliveryEffect, OutboundMessage
from apps.delivery.tasks import dispatch_outbox
from tests.delivery.conftest import DeliverySetup
from tests.delivery.smtp import FakeSMTP

pytestmark = pytest.mark.django_db(transaction=True)


def test_populated_roundtrip_preserves_retry_lineage_and_receipts(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    # Given a real refusal, authorized successor and their immutable audit history.
    smtp_server("reject")
    parent = delivery.queue()
    dispatch_outbox()
    delivery.reload(parent)
    with tenant_context(delivery.graph.tenant.pk):
        retry_dead_letter(
            delivery.graph.actor, parent.pk, expected_version=parent.version
        )
        messages = list(OutboundMessage.objects.order_by("pk").values())
        attempts = list(DeliveryAttempt.objects.order_by("pk").values())
        effects = list(DeliveryEffect.objects.order_by("pk").values())
        events = list(Event.objects.order_by("pk").values())
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        # When both new migrations are reversed against populated tables.
        MigrationExecutor(connection).migrate(
            [("delivery", "0003_outboundmessage_redacted_at_and_more")]
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regprocedure('public.delivery_outbox_health()')")
            assert cursor.fetchone() == (None,)
            cursor.execute("SELECT count(*) FROM delivery_outboundmessage")
            assert cursor.fetchone() == (2,)
        MigrationExecutor(connection).migrate(leaves)

        # Then reapplying restores the exact data, not just a nullable empty column.
        with tenant_context(delivery.graph.tenant.pk):
            assert list(OutboundMessage.objects.order_by("pk").values()) == messages
            assert list(DeliveryAttempt.objects.order_by("pk").values()) == attempts
            assert list(DeliveryEffect.objects.order_by("pk").values()) == effects
            assert list(Event.objects.order_by("pk").values()) == events
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT has_function_privilege('app_runtime', "
                "'public.delivery_outbox_health()', 'EXECUTE'), "
                "has_function_privilege('app_portal', "
                "'public.delivery_outbox_health()', 'EXECUTE')"
            )
            assert cursor.fetchone() == (True, False)
    finally:
        MigrationExecutor(connection).migrate(leaves)
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")
