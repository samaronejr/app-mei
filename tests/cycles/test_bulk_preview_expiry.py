"""A rehearsal never grants a moving selection or an expired confirmation."""

from datetime import timedelta
from threading import Event
from typing import TypedDict

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models.signals import post_save

from apps.core.tenancy import tenant_context
from apps.cycles.bulk import (
    BulkItem,
    BulkItemState,
    BulkKind,
    BulkOperation,
    BulkOperationState,
)
from apps.cycles.bulk_contracts import PreviewExpectation, Selection
from apps.cycles.errors import VersionConflict
from apps.cycles.models import CycleEvent, ServiceCycle
from apps.cycles.services.bulk import preview_bulk, run_bulk
from apps.cycles.tasks import run_bulk_operation
from apps.tenants.models import TenantRole
from tests.cycles.bulk_fixtures import bulk_setup, confirm
from tests.cycles.cycle_fixtures import PERIOD
from tests.ui.factories import add_member

pytestmark = pytest.mark.django_db(transaction=True)


class Confirmation(TypedDict):
    confirm_count: int
    exclusions: tuple[str, ...]
    expectation: PreviewExpectation


def test_preview_persists_only_the_snapshot_and_no_domain_effects() -> None:
    setup = bulk_setup()
    preview = setup.preview()
    with tenant_context(preview.tenant_id):
        assert preview.state == BulkOperationState.PREVIEW
        assert len(preview.snapshot_json["items"]) == 3
        assert preview.valid_until - preview.created_at == timedelta(minutes=15)
        assert not BulkItem.objects.exists()
        assert not ServiceCycle.objects.exists()
        assert not CycleEvent.objects.exists()


@pytest.mark.parametrize("offset", [timedelta(), timedelta(microseconds=1)])
def test_expired_preview_refuses_at_the_exact_boundary(
    monkeypatch: pytest.MonkeyPatch, offset: timedelta
) -> None:
    setup = bulk_setup("bulk-expiry")
    preview = setup.preview()
    monkeypatch.setattr(
        "apps.cycles.services.bulk.timezone.now", lambda: preview.valid_until + offset
    )
    with pytest.raises(VersionConflict):
        confirm(setup.graph.actor, preview)
    with tenant_context(preview.tenant_id):
        assert not BulkItem.objects.exists()
        assert not ServiceCycle.objects.exists()
        preview.refresh_from_db()
        assert preview.state == BulkOperationState.PREVIEW


def test_typed_count_digest_exclusions_and_version_all_bind_confirmation() -> None:
    setup = bulk_setup("bulk-confirm")
    preview = setup.preview()
    correct: Confirmation = {
        "confirm_count": 3,
        "exclusions": (),
        "expectation": PreviewExpectation(preview.version, preview.snapshot_digest),
    }
    cases: list[tuple[Confirmation, type[Exception]]] = [
        ({**correct, "confirm_count": 2}, ValidationError),
        ({**correct, "confirm_count": True}, ValidationError),
        (
            {**correct, "expectation": PreviewExpectation(preview.version, "0" * 64)},
            VersionConflict,
        ),
        (
            {**correct, "expectation": PreviewExpectation(0, preview.snapshot_digest)},
            VersionConflict,
        ),
        ({**correct, "exclusions": ("not-in-the-snapshot",)}, ValidationError),
        (
            {**correct, "exclusions": (str(setup.engagements[0].pk),) * 2},
            ValidationError,
        ),
    ]
    with tenant_context(preview.tenant_id):
        for payload, error in cases:
            with pytest.raises(error):
                run_bulk(setup.graph.actor, preview.pk, **payload)
            assert not BulkItem.objects.exists()
            assert not ServiceCycle.objects.exists()
        result = confirm(
            setup.graph.actor, preview, exclusions=(str(setup.engagements[0].pk),)
        )
    run_bulk_operation(str(preview.tenant_id), str(result.pk))
    with tenant_context(preview.tenant_id):
        assert set(ServiceCycle.objects.values_list("engagement_id", flat=True)) == {
            setup.engagements[1].pk,
            setup.engagements[2].pk,
        }


def test_execution_never_expands_the_snapshot_selection() -> None:
    setup = bulk_setup("bulk-frozen-selection")
    with tenant_context(setup.graph.tenant.pk):
        preview = preview_bulk(
            setup.graph.actor,
            BulkKind.OPEN_CYCLES,
            Selection(
                (setup.engagements[0].pk,),
                period=PERIOD,
                playbook_version_id=setup.graph.definition.pk,
            ),
        )
    operation = confirm(setup.graph.actor, preview)
    run_bulk_operation(str(operation.tenant_id), str(operation.pk))
    with tenant_context(operation.tenant_id):
        assert list(ServiceCycle.objects.values_list("engagement_id", flat=True)) == [
            setup.engagements[0].pk
        ]


def test_snapshot_identity_is_immutable_even_through_a_raw_update() -> None:
    setup = bulk_setup("bulk-immutable")
    preview = setup.preview()
    with tenant_context(preview.tenant_id):
        with pytest.raises(IntegrityError), transaction.atomic():
            BulkOperation.objects.filter(pk=preview.pk).update(snapshot_json={})
        with pytest.raises(IntegrityError), transaction.atomic():
            BulkOperation.objects.filter(pk=preview.pk).update(
                valid_until=preview.valid_until + timedelta(days=1)
            )
        preview.refresh_from_db()
        assert len(preview.snapshot_json["items"]) == 3


def test_another_operator_cannot_confirm_using_the_preview_creators_identity() -> None:
    setup = bulk_setup("bulk-confirm-person", count=1)
    preview = setup.preview()
    operator = add_member(
        setup.graph.tenant, "other-operator@bulk.test", TenantRole.OPERATIONS_ADMIN
    )
    with pytest.raises(PermissionDenied):
        confirm(operator, preview)
    with tenant_context(preview.tenant_id):
        assert not BulkItem.objects.exists()
        assert not ServiceCycle.objects.exists()


def test_expiry_during_confirmation_rolls_back_all_materialized_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = bulk_setup("bulk-confirm-expiry", count=1)
    preview = setup.preview()
    clock = [preview.created_at]
    inserted = Event()
    monkeypatch.setattr("apps.cycles.services.bulk.timezone.now", lambda: clock[0])

    def advance_clock(
        sender: type[BulkItem], instance: BulkItem, **kwargs: object
    ) -> None:
        if instance.operation_id == preview.pk:
            inserted.set()
            clock[0] = preview.valid_until

    post_save.connect(advance_clock, sender=BulkItem, weak=False)
    try:
        with pytest.raises(VersionConflict):
            confirm(setup.graph.actor, preview)
    finally:
        post_save.disconnect(advance_clock, sender=BulkItem)
    assert inserted.is_set()
    with tenant_context(preview.tenant_id):
        assert not BulkItem.objects.exists()
        preview.refresh_from_db()
        assert preview.state == BulkOperationState.PREVIEW


def test_confirmed_but_expired_work_never_executes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = bulk_setup("bulk-execute-expiry", count=1)
    operation = confirm(setup.graph.actor, setup.preview())
    monkeypatch.setattr(
        "apps.cycles.bulk_runner.timezone.now",
        lambda: operation.valid_until + timedelta(seconds=1),
    )
    run_bulk_operation(str(operation.tenant_id), str(operation.pk))
    with tenant_context(operation.tenant_id):
        assert not ServiceCycle.objects.exists()
        assert operation.items.get().state == BulkItemState.SKIPPED_CHANGED
