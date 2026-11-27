"""Each AXRM-022 reverse guard refuses with its own error and keeps the blocking row.

Replayed as the migration authority (RESET ROLE), as in test_migration_rollback.py:
each case targets the immediate predecessor with every unrelated later guard empty,
captures the outcome, and checks the row, its content and the migration remain
before the leaves are restored.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder

from apps.core.tenancy import tenant_context
from apps.cycles.models import ActionProposal
from apps.cycles.publications import ActionExecution
from apps.cycles.records import ExceptionInternal
from apps.integrations.call_models import ProviderCall
from apps.integrations.ciencia_models import CienciaRecord
from apps.integrations.models import AuthorityRecord
from tests.integrations.ciencia_storage_rows import (
    StorageWorld,
    assessment,
    ciencia_record,
    fiscal_execution,
    fiscal_proposal,
    followup,
    paired_call,
    provider_call,
    storage_world,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from pytest_django.fixtures import SettingsWrapper

pytestmark = pytest.mark.django_db(transaction=True)

CYCLES_0024 = ("cycles", "0024_fiscal_manifestation")
CYCLES_0026 = ("cycles", "0026_fiscal_kind_closed")
CYCLES_0025 = ("cycles", "0025_manifestation_followup")
INTEGRATIONS_0017 = ("integrations", "0017_ciencia_call_refs")
INTEGRATIONS_0018 = ("integrations", "0018_ciencia_professional_note")
INTEGRATIONS_0020 = ("integrations", "0020_ciencia_recovery")
BATCHES = "Ciencia batches exist; retain approvals and use forward repair."
FOLLOWUPS = "Manifestation follow-ups exist; retain them and use forward repair."
CALLS = "Ciencia calls exist; retain exact referents and use forward repair."
NOTES = "Ciencia professional notes exist; retain them and use forward repair."
RECORDS = "Ciencia recovery records exist; retain them and use forward repair."


@pytest.fixture
def world(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> StorageWorld:
    return storage_world(settings, tmp_path, monkeypatch)


def _applied(node: tuple[str, str]) -> bool:
    return node in MigrationRecorder(connection).applied_migrations()


@contextmanager
def _as_migrator() -> Iterator[None]:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        yield
    finally:
        MigrationExecutor(connection).migrate(leaves)
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")


@contextmanager
def _without_closure() -> Iterator[None]:
    """As migrator, unapply cycles 0026 so a fiscal row can be stored at all.

    The 0024 guard must still see such a row: 0026 is unapplied first (its real
    reverse order), and in `finally` the rows are truncated (frozen proposals refuse a
    row DELETE) so the leaves re-apply 0026 over an empty fiscal set.
    """
    with _as_migrator():
        MigrationExecutor(connection).migrate([CYCLES_0025])
        try:
            yield
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    "TRUNCATE cycles_actionexecution, cycles_actionproposal CASCADE"
                )


def _reverse(node: tuple[str, str], target: tuple[str, str]) -> object:
    """Catalog first, then the real backward executor; the outcome is returned."""
    assert _applied(node), node
    outcome: object = None
    try:
        MigrationExecutor(connection).migrate([target])
    except Exception as error:  # noqa: BLE001 - the outcome is the assertion
        outcome = error
    return outcome


def _refused(outcome: object, message: str, node: tuple[str, str]) -> None:
    assert isinstance(outcome, IrreversibleError), outcome
    assert str(outcome) == message
    assert _applied(node), node


def test_0024_refuses_any_frozen_fiscal_proposal(world: StorageWorld) -> None:
    assert _applied(CYCLES_0024), CYCLES_0024  # catalog first
    with _without_closure():
        row = fiscal_proposal(world)
        outcome = _reverse(CYCLES_0024, ("cycles", "0023_fiscal_document_review"))
        _refused(outcome, BATCHES, CYCLES_0024)
        with tenant_context(world.tenant_id):
            kept = ActionProposal.objects.get(pk=row.pk)
        assert (bytes(kept.manifest_bytes), kept.digest) == (
            bytes(row.manifest_bytes),
            row.digest,
        )


def test_0024_refuses_a_fiscal_kind_execution_on_a_legacy_proposal(
    world: StorageWorld,
) -> None:
    assert _applied(CYCLES_0024), CYCLES_0024  # catalog first
    with _without_closure():
        row = fiscal_execution(world)
        outcome = _reverse(CYCLES_0024, ("cycles", "0023_fiscal_document_review"))
        _refused(outcome, BATCHES, CYCLES_0024)
        with tenant_context(world.tenant_id):
            kept = ActionExecution.objects.get(pk=row.pk)
        assert (kept.action_kind, kept.proposal_id) == (
            row.action_kind,
            row.proposal_id,
        )


def test_0025_refuses_a_recorded_manifestation_followup(world: StorageWorld) -> None:
    assert _applied(CYCLES_0025), CYCLES_0025  # catalog first
    documents: list[object] = [{"chave": "35" + "0" * 42, "resolved": False}]
    row = followup(world, documents)
    with _as_migrator():
        outcome = _reverse(CYCLES_0025, CYCLES_0024)
        _refused(outcome, FOLLOWUPS, CYCLES_0025)
        with tenant_context(world.tenant_id):
            kept = ExceptionInternal.objects.get(pk=row.pk)
        assert kept.manifestation_documents == documents


def test_0025_an_empty_followup_list_does_not_block(world: StorageWorld) -> None:
    assert _applied(CYCLES_0025), CYCLES_0025  # catalog first
    row = followup(world, [])
    with _as_migrator():
        outcome = _reverse(CYCLES_0025, CYCLES_0024)
        assert outcome is None, outcome
        assert not _applied(CYCLES_0025)
    with tenant_context(world.tenant_id):
        assert ExceptionInternal.objects.get(pk=row.pk).manifestation_documents == []


@pytest.mark.parametrize(
    ("operation", "paired"),
    [("FC-CIE", True), ("FC-CIE-R", True), ("FC-CIE-R", False)],
    ids=["fc-cie-paired", "fc-cie-r-paired", "fc-cie-r-both-null"],
)
def test_0017_refuses_any_ciencia_call(
    world: StorageWorld, operation: str, paired: bool
) -> None:
    assert _applied(INTEGRATIONS_0017), INTEGRATIONS_0017  # catalog first
    row = (
        paired_call(world, operation)
        if paired
        else provider_call(world, operation, label="read-back")
    )
    with _as_migrator():
        outcome = _reverse(INTEGRATIONS_0017, ("integrations", "0016_capture_sync"))
        _refused(outcome, CALLS, INTEGRATIONS_0017)
        with tenant_context(world.tenant_id):
            kept = ProviderCall.objects.get(pk=row.pk)
        assert (kept.operation, kept.proposal_id, kept.fiscal_revision_id) == (
            row.operation,
            row.proposal_id,
            row.fiscal_revision_id,
        )


def test_0017_an_unrelated_call_without_references_does_not_block(
    world: StorageWorld,
) -> None:
    assert _applied(INTEGRATIONS_0017), INTEGRATIONS_0017  # catalog first
    row = provider_call(world, "FC-NFE", label="control")
    with _as_migrator():
        outcome = _reverse(INTEGRATIONS_0017, ("integrations", "0016_capture_sync"))
        assert outcome is None, outcome
        assert not _applied(INTEGRATIONS_0017)
    with tenant_context(world.tenant_id):
        assert ProviderCall.objects.filter(pk=row.pk).exists()


@pytest.mark.parametrize(
    "annex_ii", [False, True], ids=["note-only", "annex-ii-with-note"]
)
def test_0018_refuses_a_recorded_professional_note(
    world: StorageWorld, annex_ii: bool
) -> None:
    assert _applied(INTEGRATIONS_0018), INTEGRATIONS_0018  # catalog first
    row = assessment(world, annex_ii=annex_ii, note="recorded note")
    with _as_migrator():
        outcome = _reverse(INTEGRATIONS_0018, INTEGRATIONS_0017)
        _refused(outcome, NOTES, INTEGRATIONS_0018)
        with tenant_context(world.tenant_id):
            kept = AuthorityRecord.objects.get(pk=row.pk)
        assert (
            kept.conclusive_manifestation_annex_ii,
            kept.conclusive_manifestation_note,
        ) == (annex_ii, "recorded note")


def test_0018_a_record_without_assessment_does_not_block(world: StorageWorld) -> None:
    assert _applied(INTEGRATIONS_0018), INTEGRATIONS_0018  # catalog first
    row = assessment(world, annex_ii=False, note="")
    with _as_migrator():
        outcome = _reverse(INTEGRATIONS_0018, INTEGRATIONS_0017)
        assert outcome is None, outcome
        assert not _applied(INTEGRATIONS_0018)
    with tenant_context(world.tenant_id):
        assert AuthorityRecord.objects.filter(pk=row.pk).exists()


def test_0020_refuses_any_recovery_record(world: StorageWorld) -> None:
    assert _applied(INTEGRATIONS_0020), INTEGRATIONS_0020  # catalog first
    row = ciencia_record(world)
    with _as_migrator():
        outcome = _reverse(
            INTEGRATIONS_0020, ("integrations", "0019_ciencia_reference_constraint")
        )
        _refused(outcome, RECORDS, INTEGRATIONS_0020)
        with tenant_context(world.tenant_id):
            kept = CienciaRecord.objects.get(pk=row.pk)
        assert (
            kept.call_id,
            kept.document_key,
            kept.owner_id,
            kept.proposal_ref,
            kept.cycle_ref,
            kept.fiscal_revision_ref,
        ) == (
            row.call_id,
            row.document_key,
            row.owner_id,
            row.proposal_ref,
            row.cycle_ref,
            row.fiscal_revision_ref,
        )


def test_an_empty_storage_round_trip_keeps_an_unrelated_exception(
    world: StorageWorld,
) -> None:
    assert _applied(CYCLES_0024), CYCLES_0024  # catalog first
    row = followup(world, [])
    with _as_migrator():
        outcome = _reverse(CYCLES_0024, ("cycles", "0023_fiscal_document_review"))
        assert outcome is None, outcome
        assert not _applied(CYCLES_0024)
        assert not _applied(INTEGRATIONS_0017)
    assert _applied(CYCLES_0025)
    assert _applied(INTEGRATIONS_0020)
    with tenant_context(world.tenant_id):
        assert ExceptionInternal.objects.get(pk=row.pk).manifestation_documents == []
