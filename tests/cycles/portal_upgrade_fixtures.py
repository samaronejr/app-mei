"""Replay the data and SQL migrations at the pre-portal cutover, not HTTP bypasses."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib import import_module
from itertools import pairwise
from uuid import UUID

from django.db import connection, transaction
from django.db.migrations import Migration, RunSQL
from django.db.migrations.executor import MigrationExecutor

from apps.accounts.models import User
from apps.audit.models import Event as AuditEvent
from apps.core.tenancy import tenant_context
from apps.cycles.effects import executors
from apps.cycles.evidence import EvidenceLink, EvidenceRequest, RequestInternal
from apps.cycles.evidence_index import capture_index
from apps.cycles.models import CycleEvent, CycleState, TaskState
from apps.cycles.services.approvals import approve
from apps.cycles.services.cycle import transition_cycle, transition_task
from apps.cycles.services.proposed_transitions import propose_completion
from apps.cycles.services.requests import RequestFields, create_request, send_requests
from apps.obligations.models import Document
from tests.integrations.pgmei_support import GuideWorld

PRE_PORTAL = ("cycles", "0024_fiscal_review_successor")
PORTAL_READ = ("cycles", "0025_portal_internal_request_boundary")
PORTAL_LIFETIME = ("cycles", "0026_internal_request_lifecycle")
PORTAL_HISTORY = ("cycles", "0027_portal_request_history")


@contextmanager
def migration_owner() -> Iterator[None]:
    """DDL belongs to the test migrator. Business writes and role reads do not."""
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        try:
            yield
        finally:
            cursor.execute("SET ROLE app_runtime")


def boundary_nodes(executor: MigrationExecutor) -> list[tuple[str, str]]:
    # The regression also runs on the rejected head, with its actual two nodes.
    return [
        node
        for node in (PORTAL_READ, PORTAL_LIFETIME, PORTAL_HISTORY)
        if node in executor.loader.graph.nodes
    ]


def _original(migration: Migration, *, lookup_only: bool) -> Migration:
    """Apply the SQL that the original 0025 or 0026 actually shipped."""
    if migration.name == PORTAL_READ[1]:
        sql_steps = [op for op in migration.operations if isinstance(op, RunSQL)]
        if not lookup_only:
            cutover = import_module("apps.cycles.migrations._request_cutover")
            sql_steps.append(RunSQL(cutover.POLICIES))
    else:
        assert migration.name == PORTAL_LIFETIME[1]
        old = import_module("apps.cycles.migrations._request_lifetime")
        sql_steps = [RunSQL(old.LOCK_VISIBILITY)]

    class OriginalMigration(Migration):
        operations = sql_steps

    return OriginalMigration(migration.name, migration.app_label)


@contextmanager
def pre_portal_database(
    *,
    deployed: int = 0,
    lookup_only: bool = False,
    inspect_boundary: Callable[[str], None] | None = None,
) -> Iterator[None]:
    """Install the old boundary on an empty worker, then apply the real upgrade."""
    executor = MigrationExecutor(connection)
    nodes = [PRE_PORTAL, *boundary_nodes(executor)]
    steps = [
        (executor.loader.project_state([old]), executor.loader.get_migration(*new))
        for old, new in pairwise(nodes)
    ]
    with migration_owner():
        for state, migration in reversed(steps):
            executor.unapply_migration(state, migration)
    try:
        yield
    finally:
        for i, (state, migration) in enumerate(steps):
            forward = (
                _original(migration, lookup_only=lookup_only)
                if i < deployed
                else migration
            )
            with migration_owner():
                executor.apply_migration(state.clone(), forward)
            if inspect_boundary:
                inspect_boundary(migration.name)


@transaction.atomic
def legacy_waiver_write(
    actor: User, request_id: UUID, *, reason: str, expected_version: int
) -> EvidenceRequest:
    """Historical persisted writes of the pre-cutover approved waiver executor."""
    request = EvidenceRequest.objects.select_for_update().get(pk=request_id)
    assert request.version == expected_version
    previous = request.state
    request.state = "dispensado"
    request.version += 1
    request.save(update_fields=["state", "version"])
    companion = RequestInternal.objects.select_for_update().get(request=request)
    companion.waiver_reason = reason
    companion.version += 1
    companion.save(update_fields=["waiver_reason", "version"])
    CycleEvent.objects.create(
        tenant_id=request.tenant_id,
        client_id=request.client_id,
        cycle_id=request.cycle_id,
        actor=actor,
        type="request.waived",
        aggregate_type="EvidenceRequest",
        aggregate_id=request.pk,
        aggregate_version=request.version,
        payload={"from": previous, "to": request.state},
    )
    return request


def attach_visible_file(world: GuideWorld, request: EvidenceRequest) -> EvidenceLink:
    """A separate visible client file; it cannot confer system-guide provenance."""
    document = Document.objects.create(
        tenant_id=request.tenant_id,
        client_id=request.client_id,
        portal_visible=True,
        sha256=request.pk.hex * 2,
        content_type="application/pdf",
        byte_size=12,
        uploaded_by=world.owner,
        original_filename="fixture.pdf",
    )
    return EvidenceLink.objects.create(
        tenant_id=request.tenant_id,
        client_id=request.client_id,
        request=request,
        document=document,
        origin="staff",
        submitted_by=world.owner,
    )


def repair_reason(world: GuideWorld, request: EvidenceRequest) -> str | None:
    """Private cutover log; test the recorded reason rather than a UI label."""
    with tenant_context(world.tenant_id):
        return (
            AuditEvent.objects.filter(
                action="cycles.request_visibility_repaired",
                object_type="EvidenceRequest",
                object_id=str(request.pk),
                metadata__client_id=str(request.client_id),
                metadata__cycle_id=str(request.cycle_id),
            )
            .values_list("metadata__reason", flat=True)
            .first()
        )


def draft_history_request(world: GuideWorld, title: str) -> EvidenceRequest:
    world.cycle.refresh_from_db()
    return create_request(
        world.owner,
        world.cycle.pk,
        RequestFields(title),
        expected_version=world.cycle.version,
    )


def sent_history_requests(
    world: GuideWorld, title: str, *, count: int
) -> list[EvidenceRequest]:
    drafts = [draft_history_request(world, title) for _ in range(count)]
    world.cycle.refresh_from_db()
    send_requests(world.owner, world.cycle.pk, expected_version=world.cycle.version)
    return drafts


def omit_revision_two(world: GuideWorld) -> tuple[EvidenceRequest, EvidenceLink]:
    """Keep real creation and send-shaped revision three but no revision two."""
    gap = draft_history_request(world, "Another ordinary title")
    link = attach_visible_file(world, gap)
    CycleEvent.objects.create(
        tenant_id=gap.tenant_id,
        client_id=gap.client_id,
        cycle_id=gap.cycle_id,
        actor=world.owner,
        type="request.sent",
        aggregate_type="EvidenceRequest",
        aggregate_id=gap.pk,
        aggregate_version=3,
        payload={"from": "rascunho", "to": "solicitado"},
    )
    EvidenceRequest.objects.filter(pk=gap.pk).update(state="solicitado", version=3)
    return gap, link


def complete_guide_cycle(world: GuideWorld) -> None:
    """Execute a supported independent completion before the schema cutover."""
    cycle = world.cycle
    for task in cycle.tasks.order_by("pk"):
        task.refresh_from_db()
        if task.state not in {TaskState.ACCEPTED, TaskState.WAIVED}:
            transition_task(
                world.owner,
                task.pk,
                TaskState.WAIVED,
                reason="Finished outside this task.",
                expected_version=task.version,
            )
    for target in (
        CycleState.ACTIVE,
        CycleState.REVIEW,
        CycleState.APPROVAL,
        CycleState.EXECUTION,
    ):
        cycle.refresh_from_db()
        if cycle.state != target:
            transition_cycle(
                world.owner, cycle.pk, target, expected_version=cycle.version
            )
    cycle.refresh_from_db()
    capture_index(world.owner, cycle.pk, expected_version=cycle.version)
    cycle.refresh_from_db()
    proposal = propose_completion(
        world.staff,
        cycle.pk,
        to_state=CycleState.COMPLETED,
        reason="",
        expected_version=cycle.version,
    )
    approved = approve(world.owner, proposal.pk, expected_version=proposal.version)
    assert executors.execute(
        world.owner, proposal.pk, expected_version=approved.proposal.version
    ).executed
    cycle.refresh_from_db()
    assert cycle.state == CycleState.COMPLETED
