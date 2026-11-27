"""0019: a Ciencia call's exact references are an immediate database pair."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction

from apps.core.identifiers import uuid7
from apps.core.tenancy import tenant_context
from apps.integrations.call_models import ProviderCall
from apps.integrations.ciencia_models import CienciaRecord
from tests.integrations.ciencia_storage_rows import (
    StorageWorld,
    bare_client,
    bare_exception,
    call_fields,
    fiscal_proposal,
    issue,
    paired_call,
    provider_call,
    recovery_row,
    storage_world,
    unbound_call,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

pytestmark = pytest.mark.django_db(transaction=True)
CONSTRAINT = "providercall_ciencia_exact_refs"
SCOPED_REVISION_FK = "providercall_fiscal_revision_id_scope_fk"
RECORD = "integrations_cienciarecord"


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


def test_a_reference_to_a_missing_revision_is_refused_by_its_scoped_key_first(
    world: StorageWorld,
) -> None:
    """Ordering witness: the immediate scoped FK fires before 0019's trigger.

    A null proposal with an unknown revision violates both; PostgreSQL fires AFTER row
    triggers by name, so the scoped key's RI trigger must still sort first (23503).
    """
    fields = call_fields(world, "FC-CIE", fiscal_revision_id=uuid7(), label="ordering")
    refused: IntegrityError | None = None
    with tenant_context(world.tenant_id):
        try:
            with transaction.atomic():
                ProviderCall.objects.create(**fields)
                message = "a call naming a missing revision was accepted"
                raise AssertionError(message)
        except IntegrityError as error:
            refused = error
    assert refused is not None
    assert getattr(refused.__cause__, "sqlstate", None) == "23503", refused.__cause__
    assert (
        getattr(getattr(refused.__cause__, "diag", None), "constraint_name", None)
        == SCOPED_REVISION_FK
    )


def _deletes(world: StorageWorld, table: str, pk: UUID) -> int | DatabaseError:
    """DELETE one row as app_runtime in its own savepoint: the count, or the refusal."""
    outcome: int | DatabaseError
    with tenant_context(world.tenant_id), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE app_runtime")
            cursor.execute("SELECT current_user")
            assert cursor.fetchone() == ("app_runtime",)
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {table} WHERE id = %s", [pk])  # noqa: S608
                outcome = cursor.rowcount
        except DatabaseError as error:
            outcome = error
    return outcome


def _surviving(world: StorageWorld, table: str, pk: UUID) -> int:
    """Count the parent row under its tenant (RLS hides every row outside one)."""
    with tenant_context(world.tenant_id), connection.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM {table} WHERE id = %s", [pk])  # noqa: S608
        row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def _retained_from_any_delete() -> None:
    """Opener: the recovery table itself carries a row-level BEFORE DELETE trigger."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgrelid = %s::regclass "
            "AND NOT tgisinternal AND tgtype & 8 = 8 AND tgtype & 3 = 3",
            [RECORD],
        )
        assert cursor.fetchone() == (1,)


def _identity(world: StorageWorld, pk: UUID) -> tuple[object, ...]:
    with tenant_context(world.tenant_id):
        row = CienciaRecord.objects.get(pk=pk)
    return (
        row.pk,
        row.client_id,
        row.environment,
        row.document_key,
        row.owner_id,
        row.call_id,
        row.read_call_id,
        row.cycle_exception_id,
        row.issue_id,
        row.proposal_ref,
        row.fiscal_revision_ref,
    )


def _parent(
    world: StorageWorld, edge: str
) -> tuple[str, UUID, ProviderCall, dict[str, object]]:
    """One parent of a recovery row: (table, pk, primary call, other parent keys)."""
    if edge == "client":
        client, link = bare_client(world)
        call = unbound_call(world, link, label="bare-client")
        return "clients_clientcompany", client.pk, call, {}
    call = unbound_call(world, label=f"primary-{edge}")
    if edge == "primary-call":
        return "integrations_providercall", call.pk, call, {}
    if edge == "read-call":
        read = unbound_call(world, label="read")
        return "integrations_providercall", read.pk, call, {"read_call": read}
    if edge == "cycle-exception":
        problem = bare_exception(world)
        return (
            "cycles_exception",
            problem.pk,
            call,
            {"cycle_exception": problem},
        )
    parent = issue(world)
    return "integrations_integrationissue", parent.pk, call, {"issue": parent}


EDGES = ["client", "primary-call", "read-call", "cycle-exception", "issue"]


@pytest.mark.parametrize("edge", EDGES)
def test_a_referenced_parent_cannot_be_deleted_from_under_its_recovery_duty(
    world: StorageWorld, edge: str
) -> None:
    # Added over 8f63bbf: every composite parent key cascades; the duty must survive.
    _retained_from_any_delete()
    table, pk, call, parents = _parent(world, edge)
    record = recovery_row(world, call=call, **parents)
    before = _identity(world, record.pk)
    refused = _deletes(world, table, pk)
    assert isinstance(refused, DatabaseError), refused
    cause = refused.__cause__
    assert getattr(cause, "sqlstate", None) == "42501", cause
    assert RECORD in str(cause), cause
    assert _surviving(world, table, pk) == 1
    assert _identity(world, record.pk) == before


@pytest.mark.parametrize("edge", EDGES)
def test_an_unreferenced_parent_is_still_deletable(
    world: StorageWorld, edge: str
) -> None:
    table, pk, _call, _parents = _parent(world, edge)
    assert _deletes(world, table, pk) == 1
    assert _surviving(world, table, pk) == 0


def test_the_runtime_role_has_no_direct_delete_on_a_recovery_record(
    world: StorageWorld,
) -> None:
    record = recovery_row(world, call=unbound_call(world, label="direct"))
    refused = _deletes(world, RECORD, record.pk)
    assert isinstance(refused, DatabaseError), refused
    assert getattr(refused.__cause__, "sqlstate", None) == "42501"
    assert "permission denied" in str(refused.__cause__)
    with tenant_context(world.tenant_id):
        assert CienciaRecord.objects.filter(pk=record.pk).exists()


def test_the_owner_role_cannot_delete_a_recovery_record(world: StorageWorld) -> None:
    _retained_from_any_delete()
    record = recovery_row(world, call=unbound_call(world, label="owner"))
    refused: object = None
    with tenant_context(world.tenant_id), connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        cursor.execute("SELECT current_user <> 'app_runtime'")
        assert cursor.fetchone() == (True,)
        try:
            with transaction.atomic(), connection.cursor() as deleting:
                deleting.execute(f"DELETE FROM {RECORD} WHERE id = %s", [record.pk])  # noqa: S608
        except DatabaseError as error:
            refused = error
    assert isinstance(refused, DatabaseError), refused
    assert getattr(refused.__cause__, "sqlstate", None) == "42501"
    assert RECORD in str(refused.__cause__)
    assert _identity(world, record.pk)[0] == record.pk


def test_a_recovery_record_is_still_updatable(world: StorageWorld) -> None:
    record = recovery_row(world, call=unbound_call(world, label="update"))
    with (
        tenant_context(world.tenant_id),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("SET LOCAL ROLE app_runtime")
        cursor.execute(
            f"UPDATE {RECORD} SET version = version + 1 WHERE id = %s",  # noqa: S608
            [record.pk],
        )
        assert cursor.rowcount == 1
    with tenant_context(world.tenant_id):
        assert CienciaRecord.objects.get(pk=record.pk).version == record.version + 1
