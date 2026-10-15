"""Real database backstops for frozen subjects, item keys and portal denial."""

import pytest
from django.db import IntegrityError, connection, transaction

from apps.core.tenancy import tenant_context
from apps.cycles.bulk import BulkItem, BulkItemState
from apps.cycles.tasks import run_bulk_operation
from tests.cycles.bulk_fixtures import bulk_setup, confirm

pytestmark = pytest.mark.django_db(transaction=True)


def test_item_natural_key_and_operation_tenant_pair_are_enforced() -> None:
    first = bulk_setup("bulk-storage-a", count=1)
    second = bulk_setup("bulk-storage-b", count=1)
    operation = confirm(first.graph.actor, first.preview())
    foreign = confirm(second.graph.actor, second.preview())
    with tenant_context(operation.tenant_id):
        original = operation.items.get()
        with pytest.raises(IntegrityError), transaction.atomic():
            BulkItem.objects.create(
                tenant_id=original.tenant_id,
                client_id=original.client_id,
                operation=operation,
                subject_ref=original.subject_ref,
                expected_version=original.expected_version,
                effect_key="a" * 64,
                position=5,
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            BulkItem.objects.create(
                tenant_id=original.tenant_id,
                client_id=original.client_id,
                operation=foreign,
                subject_ref=original.subject_ref,
                expected_version=original.expected_version,
                effect_key="b" * 64,
                position=6,
            )
        assert operation.items.count() == 1


def test_item_version_identity_and_terminal_receipt_cannot_be_rewritten() -> None:
    setup = bulk_setup("bulk-storage-frozen", count=1)
    operation = confirm(setup.graph.actor, setup.preview())
    with tenant_context(operation.tenant_id):
        item = operation.items.get()
        with pytest.raises(IntegrityError), transaction.atomic():
            BulkItem.objects.filter(pk=item.pk).update(
                expected_version=item.expected_version + 1
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            BulkItem.objects.filter(pk=item.pk).update(effect_key="f" * 64)
    run_bulk_operation(str(operation.tenant_id), str(operation.pk))
    with tenant_context(operation.tenant_id):
        with pytest.raises(IntegrityError), transaction.atomic():
            BulkItem.objects.filter(pk=item.pk).update(state=BulkItemState.PENDING)
        item.refresh_from_db()
        assert item.state == BulkItemState.DONE


def test_portal_reads_nothing_even_after_an_accidental_bulk_select_grant() -> None:
    setup = bulk_setup("bulk-storage-portal", count=1)
    operation = confirm(setup.graph.actor, setup.preview())
    with tenant_context(operation.tenant_id), connection.cursor() as cursor:
        assert operation.items.count() == 1
        cursor.execute("RESET ROLE")
        cursor.execute(
            "GRANT SELECT ON cycles_bulkoperation, cycles_bulkitem TO app_portal"
        )
        cursor.execute("SET LOCAL ROLE app_portal")
        cursor.execute(
            "SELECT set_config('app.client_id', %s, true)",
            [str(setup.graph.client.pk)],
        )
        try:
            cursor.execute("SELECT id FROM cycles_bulkoperation")
            assert cursor.fetchall() == []
            cursor.execute("SELECT id FROM cycles_bulkitem")
            assert cursor.fetchall() == []
        finally:
            cursor.execute("RESET ROLE")
            cursor.execute(
                "REVOKE SELECT ON cycles_bulkoperation, cycles_bulkitem FROM app_portal"
            )
            cursor.execute("SET ROLE app_runtime")
