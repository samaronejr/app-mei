"""0019: a Ciencia call's exact references are an immediate database pair."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from django.db import IntegrityError, connection, transaction

from apps.core.tenancy import tenant_context
from apps.integrations.call_models import ProviderCall
from apps.integrations.ciencia_models import CienciaRecord
from apps.integrations.models import IntegrationIssue
from tests.integrations.ciencia_storage_rows import (
    StorageWorld,
    call_fields,
    ciencia_record,
    fiscal_proposal,
    issue,
    paired_call,
    provider_call,
    storage_world,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pytest_django.fixtures import SettingsWrapper

pytestmark = pytest.mark.django_db(transaction=True)
CONSTRAINT = "providercall_ciencia_exact_refs"
RETAINED = "cienciarecord_issue_retained"


@pytest.fixture
def world(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> StorageWorld:
    return storage_world(settings, tmp_path, monkeypatch)


def _trigger_installed() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgdeferrable FROM pg_trigger WHERE tgname = %s "
            "AND tgrelid = 'integrations_providercall'::regclass",
            [CONSTRAINT],
        )
        rows = cursor.fetchall()
    assert rows == [(False,)], rows


def _refused(world: StorageWorld, change: Callable[[], object]) -> IntegrityError:
    """Run one bad change in its own savepoint under a deferring outer transaction."""
    refused: IntegrityError | None = None
    with tenant_context(world.tenant_id), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")
        try:
            with transaction.atomic():
                change()
                message = "the bad reference change was accepted"
                raise AssertionError(message)
        except IntegrityError as error:
            refused = error
    assert refused is not None
    cause = refused.__cause__
    assert getattr(cause, "sqlstate", None) == "23514", cause
    assert getattr(getattr(cause, "diag", None), "constraint_name", None) == CONSTRAINT
    return refused


@pytest.mark.parametrize(
    "changes",
    [{"proposal": None}, {"fiscal_revision": None}, {"operation": "FC-NFE"}],
    ids=["null-proposal", "null-revision", "relabelled-fc-nfe"],
)
def test_a_bound_call_cannot_lose_or_relabel_its_references(
    world: StorageWorld, changes: dict[str, object]
) -> None:
    _trigger_installed()
    call = paired_call(world)
    before = (call.operation, call.proposal_id, call.fiscal_revision_id)
    refused: IntegrityError | None = None
    with tenant_context(world.tenant_id), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")
        try:
            with transaction.atomic():
                ProviderCall.objects.filter(pk=call.pk).update(**changes)
                message = "the bad reference change was accepted"
                raise AssertionError(message)
        except IntegrityError as error:
            refused = error
        kept = ProviderCall.objects.get(pk=call.pk)
        assert (kept.operation, kept.proposal_id, kept.fiscal_revision_id) == before
    assert refused is not None
    cause = refused.__cause__
    assert getattr(cause, "sqlstate", None) == "23514", cause
    assert getattr(getattr(cause, "diag", None), "constraint_name", None) == CONSTRAINT


@pytest.mark.parametrize(
    ("operation", "with_proposal", "with_revision"),
    [("FC-CIE", True, False), ("FC-CIE", False, True), ("FC-NFE", True, True)],
    ids=["missing-revision", "missing-proposal", "unrelated-operation"],
)
def test_an_unpaired_or_unrelated_reference_cannot_be_inserted(
    world: StorageWorld, operation: str, with_proposal: bool, with_revision: bool
) -> None:
    _trigger_installed()
    proposal = fiscal_proposal(world)
    fields = call_fields(
        world,
        operation,
        proposal=proposal if with_proposal else None,
        fiscal_revision_id=world.document.current_revision_id
        if with_revision
        else None,
        label="bad-insert",
    )
    _refused(world, lambda: ProviderCall.objects.create(**fields))
    with tenant_context(world.tenant_id):
        assert not ProviderCall.objects.filter(
            request_key=fields["request_key"]
        ).exists()


@pytest.mark.parametrize("operation", ["FC-CIE", "FC-CIE-R"])
def test_a_paired_ciencia_call_is_accepted(world: StorageWorld, operation: str) -> None:
    _trigger_installed()
    accepted: object
    try:
        accepted = paired_call(world, operation)
    except IntegrityError as error:
        accepted = error
    assert isinstance(accepted, ProviderCall), accepted
    with tenant_context(world.tenant_id):
        kept = ProviderCall.objects.get(pk=accepted.pk)
    assert kept.operation == operation
    assert kept.proposal_id is not None
    assert kept.fiscal_revision_id is not None


@pytest.mark.parametrize("operation", ["FC-NFE", "FC-CIE", "FC-CIE-R"])
def test_a_call_without_references_is_accepted_for_any_operation(
    world: StorageWorld, operation: str
) -> None:
    _trigger_installed()
    accepted: object
    try:
        accepted = provider_call(world, operation, label=f"no-refs-{operation}")
    except IntegrityError as error:
        accepted = error
    assert isinstance(accepted, ProviderCall), accepted
    with tenant_context(world.tenant_id):
        kept = ProviderCall.objects.get(pk=accepted.pk)
    assert (kept.proposal_id, kept.fiscal_revision_id) == (None, None)


def test_a_referenced_issue_cannot_be_deleted_from_under_its_recovery_duty(
    world: StorageWorld,
) -> None:
    # Added over 8f63bbf: the composite issue FK cascades; the duty must survive.
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_trigger WHERE tgname = %s", [RETAINED])
        assert cursor.fetchone() == (1,)
    parent = issue(world)
    record = ciencia_record(world, issue=parent)
    refused: object = None
    with tenant_context(world.tenant_id):
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM integrations_integrationissue WHERE id = %s",
                    [parent.pk],
                )
        except IntegrityError as error:
            refused = error
        assert isinstance(refused, IntegrityError), refused
        assert IntegrationIssue.objects.filter(pk=parent.pk).exists()
        assert CienciaRecord.objects.get(pk=record.pk).issue_id == parent.pk
