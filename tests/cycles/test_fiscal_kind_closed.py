"""No fiscal proposal or execution marker can be stored yet (cycles 0026).

AXRM-022-SCAFFOLD (re-scoped 22-04): 22-06 removes the proposal check, 22-07 the
execution check, each with a populated reverse guard. Every case runs as the runtime
role and as the owner; the legacy controls still insert.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test.utils import CaptureQueriesContext

from apps.core.tenancy import tenant_context
from apps.cycles import errors as cycle_errors
from apps.cycles.effects.executors import execute
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.models import ActionProposal, ProposalState
from apps.cycles.publications import ActionExecution
from apps.cycles.services.approvals import approve, cancel, revalidate
from tests.integrations.ciencia_fixtures import manifestation_kind
from tests.integrations.ciencia_storage_rows import (
    RowShape,
    fiscal_execution,
    fiscal_proposal,
    legacy_proposal,
    storage_world,
    stored_row,
    waiver_inputs,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from pytest_django.fixtures import SettingsWrapper

    from tests.integrations.ciencia_storage_rows import StorageWorld

pytestmark = pytest.mark.django_db(transaction=True)

CYCLES_0025 = ("cycles", "0025_manifestation_followup")
CYCLES_0026 = ("cycles", "0026_fiscal_kind_closed")
PROPOSAL_CHECK = "actionproposal_fiscal_closed"
EXECUTION_CHECK = "actionexecution_fiscal_closed"
WRITES = ("INSERT", "UPDATE", "DELETE")
ROLES = ["runtime", "owner"]


@pytest.fixture
def world(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> StorageWorld:
    return storage_world(settings, tmp_path, monkeypatch)


@contextmanager
def _as(role: str) -> Iterator[None]:
    """The runtime role is the session default; the owner is RESET ROLE."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("app_runtime",)
        if role == "owner":
            cursor.execute("RESET ROLE")
    try:
        yield
    finally:
        if role == "owner":
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")


def _insert(role: str, call: Callable[[], object]) -> object:
    with _as(role):
        try:
            return call()
        except IntegrityError as error:
            return error


def _refused_by(outcome: object, constraint: str) -> None:
    assert isinstance(outcome, IntegrityError), outcome
    cause = outcome.__cause__
    assert getattr(cause, "sqlstate", None) == "23514", cause
    assert getattr(getattr(cause, "diag", None), "constraint_name", None) == constraint


def _counts(world: StorageWorld) -> tuple[int, int]:
    with tenant_context(world.tenant_id):
        return ActionProposal.objects.count(), ActionExecution.objects.count()


@pytest.mark.parametrize("role", ROLES)
def test_a_fiscal_proposal_row_cannot_be_stored(world: StorageWorld, role: str) -> None:
    before = _counts(world)
    _refused_by(_insert(role, lambda: fiscal_proposal(world)), PROPOSAL_CHECK)
    assert _counts(world) == before


@pytest.mark.parametrize("role", ROLES)
def test_a_legacy_proposal_row_is_still_stored(world: StorageWorld, role: str) -> None:
    """Control."""
    stored = _insert(
        role, lambda: fiscal_proposal(world, kind=ActionKind.REQUEST_WAIVER)
    )
    assert isinstance(stored, ActionProposal), stored


@pytest.mark.parametrize("role", ROLES)
def test_a_fiscal_execution_marker_cannot_be_stored(
    world: StorageWorld, role: str
) -> None:
    """A fiscal marker under another intent: only a legacy proposal can hold one."""
    proposal = legacy_proposal(world)
    graph = world.graph
    before = _counts(world)

    def create() -> ActionExecution:
        with tenant_context(world.tenant_id):
            return ActionExecution.objects.create(
                tenant=graph.tenant,
                client=graph.client,
                cycle=proposal.cycle,
                proposal=proposal,
                proposal_revision=proposal.revision,
                digest=proposal.digest,
                business_intent_key="f" * 64,
                action_kind=manifestation_kind(),
                executed_by=graph.staff,
            )

    _refused_by(_insert(role, create), EXECUTION_CHECK)
    assert _counts(world) == before


@pytest.mark.parametrize("role", ROLES)
def test_a_fiscal_marker_on_a_legacy_proposal_cannot_be_stored(
    world: StorageWorld, role: str
) -> None:
    """Ruling 1: every field exact to the legacy proposal except the fiscal kind."""
    proposal = legacy_proposal(world)
    graph = world.graph
    before = _counts(world)

    def create() -> ActionExecution:
        with tenant_context(world.tenant_id):
            return ActionExecution.objects.create(
                tenant=graph.tenant,
                client=graph.client,
                cycle=proposal.cycle,
                proposal=proposal,
                proposal_revision=proposal.revision,
                digest=proposal.digest,
                business_intent_key=proposal.business_intent_key,
                action_kind=manifestation_kind(),
                executed_by=graph.staff,
            )

    _refused_by(_insert(role, create), EXECUTION_CHECK)
    assert _counts(world) == before


@pytest.mark.parametrize("role", ROLES)
def test_a_legacy_marker_is_still_stored(world: StorageWorld, role: str) -> None:
    """Control."""
    proposal = legacy_proposal(world)
    graph = world.graph

    def create() -> ActionExecution:
        with tenant_context(world.tenant_id):
            return ActionExecution.objects.create(
                tenant=graph.tenant,
                client=graph.client,
                cycle=proposal.cycle,
                proposal=proposal,
                proposal_revision=proposal.revision,
                digest=proposal.digest,
                business_intent_key=proposal.business_intent_key,
                action_kind=proposal.action_kind,
                executed_by=graph.staff,
            )

    assert isinstance(_insert(role, create), ActionExecution)


# The closure refuses existing fiscal rows and rewrites nothing.


def _applied(node: tuple[str, str]) -> bool:
    return node in MigrationRecorder(connection).applied_migrations()


@contextmanager
def _closure_unapplied() -> Iterator[None]:
    """As migrator: 0026 unapplied; in `finally` the rows go (TRUNCATE: frozen
    proposals refuse a row DELETE) and the leaves are re-applied.
    """
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        MigrationExecutor(connection).migrate([CYCLES_0025])
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "TRUNCATE cycles_actionexecution, cycles_actionproposal CASCADE"
            )
        MigrationExecutor(connection).migrate(leaves)
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")


@pytest.mark.parametrize("row", ["proposal", "marker"])
def test_the_closure_migration_refuses_existing_fiscal_rows(
    world: StorageWorld, row: str
) -> None:
    graph = MigrationExecutor(connection).loader.graph
    assert CYCLES_0026 in graph.nodes, "no closure migration"
    with _closure_unapplied():
        stored = (
            fiscal_proposal(world) if row == "proposal" else fiscal_execution(world)
        )
        model = ActionProposal if row == "proposal" else ActionExecution
        outcome: object = None
        try:
            MigrationExecutor(connection).migrate([CYCLES_0026])
        except Exception as error:  # noqa: BLE001 - the outcome is the assertion
            outcome = error
        applied = _applied(CYCLES_0026)
        with tenant_context(world.tenant_id):
            kept = model.objects.get(pk=stored.pk)
    assert outcome is not None, "the closure accepted an existing fiscal row"
    assert "Fiscal manifestation rows exist" in str(outcome), outcome
    assert applied is False
    assert kept.action_kind == stored.action_kind


# No fiscal path through a legacy row: its blob may name the kind, nothing runs.

LEGACY_COMMANDS = ["approve", "cancel", "revalidate-system", "execute"]


def _fiscal_blob(world: StorageWorld) -> dict[str, object]:
    return {**waiver_inputs(world).as_data(), "action_kind": "fiscal_manifestation"}


@pytest.mark.parametrize("command", LEGACY_COMMANDS)
def test_a_legacy_row_whose_blob_names_the_fiscal_kind_runs_nothing(
    world: StorageWorld, command: str
) -> None:
    """Control at edacd36; at the re-scoped head the parent decoder refuses it."""
    state = ProposalState.APPROVED if command == "execute" else ProposalState.AWAITING
    row = stored_row(
        world.graph,
        world.capture.setup.cycle,
        RowShape(kind=ActionKind.REQUEST_WAIVER, blob=_fiscal_blob(world)),
        state,
    )
    graph = world.graph
    calls: dict[str, Callable[[], object]] = {
        "approve": lambda: approve(graph.actor, row.pk, expected_version=row.version),
        "cancel": lambda: cancel(graph.actor, row.pk, expected_version=row.version),
        "revalidate-system": lambda: revalidate(None, row.pk),
        "execute": lambda: execute(graph.actor, row.pk, expected_version=row.version),
    }
    before = _counts(world)
    outcome: object
    with (
        tenant_context(world.tenant_id),
        CaptureQueriesContext(connection) as queries,
    ):
        try:
            outcome = calls[command]()
        except Exception as error:  # noqa: BLE001 - the outcome is the assertion
            outcome = error
    assert isinstance(outcome, ValidationError | cycle_errors.IllegalTransition), (
        outcome
    )
    written = [
        query["sql"]
        for query in queries.captured_queries
        if query["sql"].lstrip().upper().startswith(WRITES)
    ]
    assert written == [], written
    assert _counts(world) == before
    with tenant_context(world.tenant_id):
        assert ActionProposal.objects.get(pk=row.pk).state == state
