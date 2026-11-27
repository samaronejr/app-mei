"""One proposal-relative four-state marker classification, read by every surface.

Added over 8f63bbf (AXRM-022 part 4, repair 3; kept). Every marker is compared with the
TARGET proposal: CONFLICT when any marker attached to it disagrees with it (this takes
precedence, even beside an exact marker); EXACT when one agrees; OCCUPIED when none is
attached but one holds its (tenant, client, business intent); otherwise ABSENT.
Reviewer rulings 1-3 (2026-11-27) fix what each state permits.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.http import Http404
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.authz.models import Role
from apps.clients.models import ClientAssignment, ClientContact
from apps.core.identifiers import uuid7
from apps.core.tenancy import tenant_context
from apps.cycles import errors as cycle_errors
from apps.cycles.bulk import BulkItem, BulkItemState, BulkKind
from apps.cycles.bulk_contracts import Selection
from apps.cycles.completion_checks import completion_items
from apps.cycles.completion_records import CompletionGeneration
from apps.cycles.display_completion import display_completion_items
from apps.cycles.display_reads import load_cycle_detail
from apps.cycles.effects import build
from apps.cycles.effects.build import BuildError
from apps.cycles.effects.executors import execute
from apps.cycles.effects.guards import actions_terminal, effects_started
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind, canonicalize
from apps.cycles.evidence_index import capture_index
from apps.cycles.index_projection import collect_index
from apps.cycles.models import (
    ActionExecution,
    ActionProposal,
    Approval,
    ClientPublication,
    CycleEvent,
    CycleState,
    EvidenceIndex,
    EvidenceRequest,
    ProposalState,
    PublicationState,
    RequestState,
    ServiceCycle,
)
from apps.cycles.models import Exception as CycleException
from apps.cycles.services.approvals import (
    _intent,
    approve,
    cancel,
    propose,
    reject,
    revalidate,
)
from apps.cycles.services.bulk import preview_bulk
from apps.cycles.services.completion import (
    apply_completion,
    freeze_completion,
    propose_reopen,
)
from apps.cycles.services.cycle import open_cycle, transition_cycle
from apps.cycles.services.proposed_transitions import propose_waiver
from apps.cycles.services.requests import RequestFields, create_request, waive_request
from apps.cycles.tasks import run_bulk_operation
from apps.delivery.errors import IntentConflict
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.tasks import dispatch_tenant
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.cycles.approval_fixtures import approval_graph
from tests.cycles.completion_fixtures import (
    CompletionSetup,
    close_cycle,
    completion_setup,
)
from tests.cycles.publication_fixtures import publication_graph
from tests.cycles.test_approval_revocation import reviewable
from tests.cycles.test_execution_marker_kind import (
    OTHER_INTENT,
    _session,
    _withdrawal,
    marker,
    wrong_kind,
)
from tests.cycles.test_fiscal_manifestation_action import _manifest
from tests.cycles.test_fiscal_manifestation_closed import (
    Closed,
    _as_eligible_before_this_part,
    _queued_before_this_part,
)
from tests.cycles.test_fiscal_manifestation_closed import _session as closed_session
from tests.integrations.ciencia_storage_rows import (
    RowShape,
    fiscal_proposal,
    legacy_proposal,
    storage_world,
    stored_row,
    waiver_inputs,
)
from tests.portal.request_fixtures import portal_setup

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from tests.cycles.approval_fixtures import ApprovalGraph
    from tests.integrations.ciencia_storage_rows import StorageWorld

pytestmark = pytest.mark.django_db(transaction=True)
WRITES = ("INSERT", "UPDATE", "DELETE")
REFUSAL = "fiscal_manifestation_unavailable"


@pytest.fixture
def closed(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Closed:
    return Closed(storage_world(settings, tmp_path, monkeypatch))


@pytest.fixture
def world(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> StorageWorld:
    return storage_world(settings, tmp_path, monkeypatch)


def _attempt(tenant: UUID, call: Callable[[], object]) -> tuple[object, list[str]]:
    """Capture the WHOLE command: its outcome and every statement it attempted."""
    outcome: object
    with tenant_context(tenant), CaptureQueriesContext(connection) as queries:
        try:
            outcome = call()
        except Exception as error:  # noqa: BLE001 - the outcome is the assertion
            outcome = error
    return outcome, [query["sql"] for query in queries.captured_queries]


def _no_dml(statements: list[str]) -> None:
    """No attempted INSERT/UPDATE/DELETE, even one a savepoint later rolled back."""
    written = [sql for sql in statements if sql.lstrip().upper().startswith(WRITES)]
    assert written == [], written


def _state(tenant: UUID, proposal: ActionProposal) -> tuple[object, ...]:
    with tenant_context(tenant):
        row = ActionProposal.objects.get(pk=proposal.pk)
        return (
            row.state,
            row.version,
            row.invalidation_reasons,
            sorted(
                ActionExecution.objects.values_list(
                    "pk", "proposal_id", "business_intent_key", "action_kind"
                )
            ),
            ActionProposal.objects.count(),
            Approval.objects.count(),
            CycleEvent.objects.count(),
            OutboundMessage.objects.count(),
            CycleException.objects.count(),
        )


def _cycle(setup: ApprovalGraph) -> ServiceCycle:
    with tenant_context(setup.graph.tenant.pk):
        return ServiceCycle.objects.get(pk=setup.cycle.pk)


def _revision(setup: ApprovalGraph, previous: ActionProposal) -> ProposalDraft:
    return ProposalDraft(
        ProposalInput.from_data(previous.input_spec),
        previous.expires_at,
        previous_id=previous.pk,
        previous_version=previous.version,
    )


# Section 7.1: one classification, compared with the target, agreed by every reader.

STATES = {
    "absent": "absent",
    "exact": "exact",
    "lone-wrong": "conflict",
    "exact-wrong-kind": "conflict",
    "exact-same-kind": "conflict",
    "occupied": "occupied",
    "wrong-cycle": "conflict",
    "other-client": "absent",
}


def _history(history: str, setup: ApprovalGraph, slug: str) -> None:
    proposal = setup.proposal
    if history == "exact":
        marker(proposal)
    elif history == "lone-wrong":
        marker(proposal, action_kind=wrong_kind(proposal))
    elif history == "exact-wrong-kind":
        marker(proposal)
        marker(
            proposal,
            action_kind=wrong_kind(proposal),
            business_intent_key=OTHER_INTENT,
        )
    elif history == "exact-same-kind":
        marker(proposal)
        marker(proposal, business_intent_key=OTHER_INTENT)
    elif history == "occupied":
        # The superseded revision under the same natural intent holds the key.
        with tenant_context(setup.graph.tenant.pk):
            previous = ActionProposal.objects.get(pk=proposal.previous_id or uuid4())
        marker(previous)
    elif history == "wrong-cycle":
        graph = setup.graph
        with tenant_context(graph.tenant.pk):
            other = open_cycle(
                graph.actor,
                graph.engagement.pk,
                setup.cycle.period_start.replace(
                    month=setup.cycle.period_start.month + 1
                ),
                graph.definition.pk,
                idempotency_key=f"{slug}-next",
            )
        marker(proposal, cycle_id=other.id)
    elif history == "other-client":
        foreign = reviewable(f"{slug}-foreign")
        marker(foreign.proposal, business_intent_key=proposal.business_intent_key)


@pytest.mark.parametrize("history", list(STATES))
def test_one_classification_decides_every_reader(history: str) -> None:
    slug = f"state-{history}"
    setup = reviewable(slug)
    proposal = setup.proposal
    tenant = setup.graph.tenant.pk
    _history(history, setup, slug)
    state = STATES[history]
    key = f"actions:{proposal.pk}:{proposal.version}"
    with tenant_context(tenant):
        cycle = ServiceCycle.objects.get(pk=setup.cycle.pk)
        locked = key in {item.key for item in completion_items(cycle)}
        reads = load_cycle_detail(setup.graph.actor, cycle.pk)
        displayed = key in {item.key for item in display_completion_items(reads)}
        terminal = actions_terminal(cycle)
        raw = len(collect_index(cycle)["executions"])  # type: ignore[arg-type]
        stored = ActionExecution.objects.filter(cycle=cycle).count()
    assert (locked, displayed, terminal) == (
        state != "exact",
        state != "exact",
        state == "exact",
    )
    assert raw == stored  # the raw evidence index keeps every marker, unclassified
    markers = importlib.import_module("apps.cycles.effects.markers")
    annotate = getattr(markers, "with_marker_state", None)
    assert annotate is not None, "no proposal-relative classification"
    with tenant_context(tenant):
        single = markers.marker_state(proposal)[0]
        with CaptureQueriesContext(connection) as queries:
            rows = dict(
                annotate(
                    ActionProposal.objects.filter(cycle_id=proposal.cycle_id)
                ).values_list("pk", "marker_state")
            )
    assert len(queries.captured_queries) == 1
    assert single == rows[proposal.pk] == state


# Section 7.2: a conflict is refused by every command, under the locks, with no DML.

CONFLICTS = ["lone-wrong", "mixed", "email-other-key"]


def _conflict(history: str, proposal: ActionProposal) -> None:
    if history == "lone-wrong":
        marker(proposal, action_kind=ActionKind.REQUEST_WAIVER)
    elif history == "mixed":
        marker(proposal)
        marker(proposal, business_intent_key=OTHER_INTENT)
    else:
        marker(proposal, business_intent_key=OTHER_INTENT)


AWAITING_COMMANDS = [
    "approve",
    "reject",
    "cancel",
    "revalidate-actor",
    "revalidate-system",
    "supersede",
]


@pytest.mark.parametrize("history", CONFLICTS)
@pytest.mark.parametrize("command", AWAITING_COMMANDS)
def test_a_conflicted_proposal_refuses_every_command_without_writing(
    command: str, history: str
) -> None:
    setup = approval_graph(f"conflict-{command}-{history}")
    proposal = setup.proposal
    graph = setup.graph
    tenant = graph.tenant.pk
    _conflict(history, proposal)
    before = _state(tenant, proposal)
    calls: dict[str, Callable[[], object]] = {
        "approve": lambda: approve(
            graph.actor, proposal.pk, expected_version=proposal.version
        ),
        "reject": lambda: reject(
            graph.actor, proposal.pk, expected_version=proposal.version, reason="Não."
        ),
        "cancel": lambda: cancel(
            graph.staff, proposal.pk, expected_version=proposal.version
        ),
        "revalidate-actor": lambda: revalidate(graph.actor, proposal.pk),
        "revalidate-system": lambda: revalidate(None, proposal.pk),
        "supersede": lambda: propose(
            graph.staff,
            setup.cycle.pk,
            _revision(setup, proposal),
            expected_version=ServiceCycle.objects.get(pk=setup.cycle.pk).version,
        ),
    }
    outcome, statements = _attempt(tenant, calls[command])
    assert isinstance(outcome, IntentConflict), outcome
    _no_dml(statements)
    assert _state(tenant, proposal) == before


def test_an_expired_conflicted_proposal_is_refused_before_its_expiry_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = approval_graph("conflict-expired")
    proposal = setup.proposal
    tenant = setup.graph.tenant.pk
    _conflict("lone-wrong", proposal)
    before = _state(tenant, proposal)
    later = timezone.now() + timedelta(days=3)
    monkeypatch.setattr(timezone, "now", lambda: later)
    outcome, statements = _attempt(
        tenant,
        lambda: approve(
            setup.graph.actor, proposal.pk, expected_version=proposal.version
        ),
    )
    monkeypatch.undo()
    assert isinstance(outcome, IntentConflict), outcome
    _no_dml(statements)
    assert _state(tenant, proposal) == before


@pytest.mark.parametrize("history", CONFLICTS)
@pytest.mark.parametrize("command", ["execute", "cancel"])
def test_an_approved_conflicted_proposal_is_neither_run_nor_withdrawn(
    command: str, history: str
) -> None:
    setup = reviewable(f"conflict-approved-{command}-{history}")
    proposal = setup.proposal
    graph = setup.graph
    tenant = graph.tenant.pk
    _conflict(history, proposal)
    before = _state(tenant, proposal)
    calls: dict[str, Callable[[], object]] = {
        "execute": lambda: execute(
            graph.actor, proposal.pk, expected_version=proposal.version
        ),
        "cancel": lambda: cancel(
            graph.actor, proposal.pk, expected_version=proposal.version
        ),
    }
    outcome, statements = _attempt(tenant, calls[command])
    assert isinstance(outcome, IntentConflict), outcome
    _no_dml(statements)
    assert _state(tenant, proposal) == before


@pytest.mark.parametrize(
    "action", ["approve", "reject", "cancel", "revalidate", "execute"]
)
def test_a_forged_decision_on_a_conflicted_card_is_a_handled_refusal(
    settings: SettingsWrapper, action: str
) -> None:
    setup = (
        reviewable(f"conflict-card-{action}")
        if action == "execute"
        else approval_graph(f"conflict-card-{action}")
    )
    proposal = setup.proposal
    tenant = setup.graph.tenant.pk
    _conflict("lone-wrong", proposal)
    before = _state(tenant, proposal)
    response = _session(setup, settings).post(
        reverse("approval-card", args=[proposal.pk]),
        {"action": action, "expected_version": proposal.version, "reason": "Não."},
    )
    assert response.status_code == 422
    assert _state(tenant, proposal) == before


def test_bulk_marks_a_conflict_ineligible_and_refuses_an_older_eligible_preview() -> (
    None
):
    setup = approval_graph("conflict-bulk")
    graph = setup.graph
    tenant = graph.tenant
    tenant.features = [*tenant.features, "bulk_r1"]
    tenant.save(update_fields=["features"])
    proposal = setup.proposal
    _conflict("lone-wrong", proposal)
    with tenant_context(tenant.pk):
        operation = preview_bulk(
            graph.actor, BulkKind.APPROVE_ITEMS, Selection((proposal.pk,))
        )
    item = operation.snapshot_json["items"][0]
    assert (item["eligible"], item["reason"]) == (False, "execution_conflict")
    stale = _as_eligible_before_this_part(operation, proposal.pk)
    _queued_before_this_part(stale)
    before = _state(tenant.pk, proposal)[:4]
    run_bulk_operation(str(tenant.pk), str(stale.pk))
    with tenant_context(tenant.pk):
        refused = BulkItem.objects.get(operation=stale)
        approvals = Approval.objects.filter(proposal=proposal).count()
    assert refused.state == BulkItemState.FAILED
    assert refused.result_json.get("code") == "execution_refused"
    assert approvals == 0
    assert _state(tenant.pk, proposal)[:4] == before


def _occupied(slug: str) -> tuple[ApprovalGraph, ActionProposal]:
    """Run revision 1 for real, then revise: revision 2 is OCCUPIED (ruling 1)."""
    setup = approval_graph(slug)
    graph = setup.graph
    with tenant_context(graph.tenant.pk):
        approve(graph.actor, setup.proposal.pk, expected_version=1)
        first = execute(graph.actor, setup.proposal.pk, expected_version=2).proposal
        cycle = ServiceCycle.objects.get(pk=setup.cycle.pk)
        second = propose(
            graph.staff,
            cycle.pk,
            ProposalDraft(
                replace(
                    ProposalInput.from_data(first.input_spec),
                    body="Outra cópia autorizada.",
                ),
                first.expires_at,
                previous_id=first.pk,
                previous_version=first.version,
            ),
            expected_version=cycle.version,
        )
    assert second.state == ProposalState.AWAITING
    return setup, second


@pytest.mark.parametrize("command", ["approve", "reject", "cancel", "revise"])
def test_an_occupied_proposal_keeps_its_decisions(command: str) -> None:
    setup, proposal = _occupied(f"occupied-{command}")
    graph = setup.graph
    with tenant_context(graph.tenant.pk):
        if command == "approve":
            result = approve(graph.actor, proposal.pk, expected_version=1).proposal
            assert result.state == ProposalState.APPROVED
        elif command == "reject":
            result = reject(
                graph.actor, proposal.pk, expected_version=1, reason="Não."
            ).proposal
            assert result.state == ProposalState.REJECTED
        elif command == "cancel":
            result = cancel(graph.staff, proposal.pk, expected_version=1).proposal
            assert result.state == ProposalState.CANCELLED
        else:
            cycle = ServiceCycle.objects.get(pk=setup.cycle.pk)
            revised = propose(
                graph.staff,
                cycle.pk,
                _revision(setup, proposal),
                expected_version=cycle.version,
            )
            assert revised.revision == proposal.revision + 1


def test_the_card_keeps_an_occupied_proposal_decidable_but_not_executable(
    settings: SettingsWrapper,
) -> None:
    setup, proposal = _occupied("occupied-card")
    response = _session(setup, settings).get(
        reverse("approval-card", args=[proposal.pk])
    )
    assert response.status_code == 200
    page = response.content.decode()
    assert response.context["execution"] is None
    assert response.context["may_execute"] is False
    assert response.context["may_approve"] is True
    assert response.context["awaiting"] is True
    assert 'value="approve"' in page
    assert 'value="execute"' not in page
    assert "data-marker-occupied" in page
    assert "data-marker-conflict" not in page


def test_an_occupied_proposal_is_never_executed() -> None:
    setup, proposal = _occupied("occupied-execute")
    graph = setup.graph
    tenant = graph.tenant.pk
    with tenant_context(tenant):
        approved = approve(graph.actor, proposal.pk, expected_version=1).proposal
    before = _state(tenant, approved)
    outcome, statements = _attempt(
        tenant,
        lambda: execute(graph.actor, approved.pk, expected_version=approved.version),
    )
    assert isinstance(outcome, IntentConflict), outcome
    _no_dml(statements)
    assert _state(tenant, approved) == before


@pytest.mark.parametrize("started", [False, True], ids=["before-smtp", "send-started"])
def test_an_exact_queued_email_is_cancelled_only_before_its_send_starts(
    started: bool,
) -> None:
    setup = reviewable(f"exact-email-cancel-{started}")
    graph = setup.graph
    with tenant_context(graph.tenant.pk):
        executed = execute(
            graph.actor, setup.proposal.pk, expected_version=setup.proposal.version
        ).proposal
        if started:
            OutboundMessage.objects.filter(proposal_id=executed.pk).update(
                send_started_at=timezone.now()
            )
        # An exact queued email is no started effect until its send starts (ruling 3).
        cycle = ServiceCycle.objects.get(pk=setup.cycle.pk)
        assert effects_started(cycle) is started
        outcome: object
        try:
            outcome = cancel(
                graph.actor, executed.pk, expected_version=executed.version
            )
        except cycle_errors.IllegalTransition as error:
            outcome = error
    if started:
        assert isinstance(outcome, cycle_errors.IllegalTransition), outcome
    else:
        assert not isinstance(outcome, cycle_errors.IllegalTransition), outcome
        assert outcome.proposal.state == ProposalState.CANCELLED


def test_an_exact_waiver_cannot_be_cancelled(world: StorageWorld) -> None:
    proposal = legacy_proposal(world)
    graph = world.graph
    with tenant_context(world.tenant_id):
        approved = approve(graph.actor, proposal.pk, expected_version=1).proposal
        executed = execute(
            graph.staff, approved.pk, expected_version=approved.version
        ).proposal
        with pytest.raises(cycle_errors.IllegalTransition):
            cancel(graph.staff, executed.pk, expected_version=executed.version)


def test_a_waiver_receipt_executed_after_its_approval_expired_proves_nothing(
    world: StorageWorld,
) -> None:
    proposal = legacy_proposal(world)
    graph = world.graph
    with tenant_context(world.tenant_id):
        approved = approve(
            graph.actor, proposal.pk, expected_version=proposal.version
        ).proposal
        request = EvidenceRequest.objects.get(pk=world.capture.setup.request.pk)
        waive_request(
            graph.staff,
            request.pk,
            reason="Synthetic reason",
            expected_version=request.version,
        )
        expiry = Approval.objects.get(proposal=approved).expires_at
    marker(approved, executed_at=expiry + timedelta(minutes=1))
    with tenant_context(world.tenant_id):
        request.refresh_from_db()
        cycle = ServiceCycle.objects.get(pk=request.cycle_id)
        blocked = {item.key for item in completion_items(cycle)}
    assert request.state == RequestState.WAIVED
    assert f"requests:{request.pk}:{request.version}" in blocked


# Section 7.3 (ruling 2): the withdrawal rebuild reads only this proposal's own proof.


@pytest.mark.parametrize("history", ["mixed", "exact-email-proof", "exact"])
def test_the_withdrawal_rebuild_needs_this_proposal_s_exact_publication_proof(
    history: str,
) -> None:
    world = publication_graph(f"withdrawal-rebuild-{history}")
    proposal = _withdrawal(world)
    graph = world.graph
    with tenant_context(graph.tenant.pk):
        target = ClientPublication.objects.get(pk=world.proposal.pk)
        ClientPublication.objects.filter(pk=target.pk).update(
            state=PublicationState.WITHDRAWN,
            withdrawn_at=timezone.now(),
            version=target.version + 1,
        )
        spec = ProposalInput.from_data(proposal.input_spec)
    subject = proposal
    if history == "exact-email-proof":
        with tenant_context(graph.tenant.pk):
            cycle = ServiceCycle.objects.get(pk=world.cycle.pk)
            subject = propose(
                graph.staff,
                cycle.pk,
                ProposalDraft(
                    ProposalInput(
                        action_kind=ActionKind.EMAIL_NOTICE,
                        step_key="withdrawal-email",
                        recipient_ids=(world.contact.pk,),
                        subject="Aviso",
                        body="Aviso aprovado.",
                    ),
                    timezone.now() + timedelta(days=1),
                ),
                expected_version=cycle.version,
            )
    marker(subject)
    if history == "mixed":
        marker(subject, business_intent_key=OTHER_INTENT)
    outcome: object
    with tenant_context(graph.tenant.pk):
        tenant = Tenant.objects.get(pk=graph.tenant.pk)
        try:
            outcome = build._publication_payload(subject, spec, tenant, ())
        except BuildError as error:
            outcome = error
    if history == "exact":
        assert isinstance(outcome, dict), outcome
        assert outcome["withdrawal"]["version"] == target.version
    else:
        assert isinstance(outcome, BuildError), outcome
        assert outcome.code == "publication_unavailable"


# Section 7.4 (ruling 3): mixed history alone does not establish a started effect.


@pytest.mark.parametrize("history", ["mixed", "other-exact"])
def test_cycle_cancellation_is_blocked_only_by_proved_history(
    world: StorageWorld, history: str
) -> None:
    waiver = legacy_proposal(world)
    if history == "mixed":
        marker(waiver)
        marker(waiver, business_intent_key=OTHER_INTENT)
    else:
        marker(waiver)
        conflicted = fiscal_proposal(world, kind=ActionKind.EMAIL_NOTICE)
        marker(conflicted, action_kind=ActionKind.REQUEST_WAIVER)
    graph = world.graph
    with tenant_context(world.tenant_id):
        cycle = ServiceCycle.objects.get(pk=waiver.cycle_id)
        started = effects_started(cycle)
        outcome: object
        try:
            outcome = transition_cycle(
                graph.actor,
                cycle.pk,
                CycleState.CANCELLED,
                expected_version=cycle.version,
                reason="Cancelado pelo escritório.",
            )
        except cycle_errors.IllegalTransition as error:
            outcome = error
        after = ServiceCycle.objects.get(pk=cycle.pk).state
    if history == "mixed":
        assert started is False
        assert after == CycleState.CANCELLED
    else:
        assert started is True
        assert isinstance(outcome, cycle_errors.IllegalTransition), outcome


def test_cycle_cancellation_is_blocked_by_a_real_send_start() -> None:
    setup = reviewable("cancel-after-send")
    graph = setup.graph
    with tenant_context(graph.tenant.pk):
        execute(graph.actor, setup.proposal.pk, expected_version=setup.proposal.version)
        OutboundMessage.objects.filter(proposal_id=setup.proposal.pk).update(
            send_started_at=timezone.now()
        )
        cycle = ServiceCycle.objects.get(pk=setup.cycle.pk)
        assert effects_started(cycle) is True
        with pytest.raises(cycle_errors.IllegalTransition):
            transition_cycle(
                graph.actor,
                cycle.pk,
                CycleState.CANCELLED,
                expected_version=cycle.version,
                reason="Cancelado pelo escritório.",
            )


def _reopening(
    slug: str,
) -> tuple[CompletionSetup, User, ActionProposal, ServiceCycle]:
    setup = completion_setup(slug)
    graph = setup.graph
    with tenant_context(graph.tenant.pk):
        close_cycle(setup)
        cycle = ServiceCycle.objects.get(pk=setup.cycle.pk)
        reviewer = User.objects.create_user(email=f"{slug}@example.test")
        Membership.objects.create(
            tenant=graph.tenant, user=reviewer, role=Role.OPERATIONS_ADMIN
        )
        reopening = propose_reopen(
            graph.actor,
            cycle.pk,
            reason="Conferir uma evidência nova.",
            affected_scope=(cycle.pk,),
            expected_version=cycle.version,
        )
        decided = approve(
            reviewer, reopening.pk, expected_version=reopening.version
        ).proposal
        cycle.refresh_from_db()
    return setup, reviewer, decided, cycle


def test_a_conflicted_reopening_is_refused_before_any_write() -> None:
    _setup, reviewer, decided, cycle = _reopening("conflicted-reopening")
    marker(decided, business_intent_key=OTHER_INTENT)
    tenant = cycle.tenant_id
    before = _state(tenant, decided)
    outcome, statements = _attempt(
        tenant,
        lambda: execute(reviewer, decided.pk, expected_version=decided.version),
    )
    assert isinstance(outcome, IntentConflict), outcome
    _no_dml(statements)
    assert _state(tenant, decided) == before


def test_a_reopening_with_a_pending_fiscal_sibling_attempts_no_write() -> None:
    setup, reviewer, decided, cycle = _reopening("fiscal-sibling-no-dml")
    sibling = stored_row(setup.graph, cycle, RowShape())
    tenant = cycle.tenant_id
    with tenant_context(tenant):
        decided = ActionProposal.objects.get(pk=decided.pk)
        cycle = ServiceCycle.objects.get(pk=cycle.pk)
    before = _state(tenant, sibling)
    outcome, statements = _attempt(
        tenant,
        lambda: execute(reviewer, decided.pk, expected_version=decided.version),
    )
    assert isinstance(outcome, ValidationError), outcome
    assert outcome.code == REFUSAL
    _no_dml(statements)
    assert _state(tenant, sibling) == before
    with tenant_context(tenant):
        after = ServiceCycle.objects.get(pk=cycle.pk)
    assert (after.state, after.amendment, after.version) == (
        cycle.state,
        cycle.amendment,
        cycle.version,
    )


def test_the_reopening_keeps_its_own_sibling_check() -> None:
    """A direct domain call past the executor still meets _reopen's own refusal."""
    setup, reviewer, decided, cycle = _reopening("fiscal-sibling-defensive")
    stored_row(setup.graph, cycle, RowShape())
    marker(decided)
    outcome: object = None
    with tenant_context(cycle.tenant_id):
        try:
            with transaction.atomic():
                apply_completion(reviewer, ActionProposal.objects.get(pk=decided.pk))
        except ValidationError as error:
            outcome = error
        after = ServiceCycle.objects.get(pk=cycle.pk)
    assert isinstance(outcome, ValidationError), outcome
    assert outcome.code == REFUSAL
    assert (after.state, after.amendment) == (cycle.state, cycle.amendment)


# Section 7.5: authorization before disclosure; one stored-input reader.

OUTSIDER_COMMANDS = ["approve", "reject", "cancel", "revalidate", "execute"]


def _outsider(closed: Closed) -> User:
    graph = closed.world.setup.graph
    with tenant_context(closed.world.tenant_id):
        user = User.objects.create_user(
            email=f"outsider-{uuid4().hex[:8]}@example.test"
        )
        Membership.objects.create(
            tenant=graph.tenant, user=user, role=TenantRole.STAFF_ACCOUNTANT
        )
    return user


def _command(name: str, actor: User, pk: UUID, version: int) -> Callable[[], object]:
    return {
        "approve": lambda: approve(actor, pk, expected_version=version),
        "reject": lambda: reject(actor, pk, expected_version=version, reason="Não."),
        "cancel": lambda: cancel(actor, pk, expected_version=version),
        "revalidate": lambda: revalidate(actor, pk),
        "execute": lambda: execute(actor, pk, expected_version=version),
    }[name]


@pytest.mark.parametrize("command", OUTSIDER_COMMANDS)
def test_an_unassigned_actor_cannot_tell_a_stored_row_from_a_missing_one(
    closed: Closed, command: str
) -> None:
    state = ProposalState.APPROVED if command == "execute" else ProposalState.AWAITING
    row = fiscal_proposal(closed.storage, state)
    outsider = _outsider(closed)
    tenant = closed.world.tenant_id
    before = _state(tenant, row)
    seen, seen_sql = _attempt(tenant, _command(command, outsider, row.pk, row.version))
    missing, missing_sql = _attempt(tenant, _command(command, outsider, uuid4(), 1))
    assert isinstance(seen, Http404), seen
    assert isinstance(missing, Http404), missing
    _no_dml(seen_sql + missing_sql)
    assert _state(tenant, row) == before


def test_the_system_path_skips_the_portfolio_check(closed: Closed) -> None:
    """The trusted dispatcher reads as no one; an unassigned initiator is moot."""
    row = fiscal_proposal(closed.storage)
    graph = closed.world.setup.graph
    tenant = closed.world.tenant_id
    with tenant_context(tenant):
        ClientAssignment.objects.filter(client=graph.client, user=graph.staff).delete()
    outcome, statements = _attempt(tenant, lambda: revalidate(None, row.pk))
    assert isinstance(outcome, ValidationError), outcome
    assert outcome.code == REFUSAL
    _no_dml(statements)


def test_a_revision_by_someone_who_may_not_revise_learns_nothing(
    closed: Closed,
) -> None:
    spec = waiver_inputs(closed.storage)
    cycle = closed.cycle
    row = fiscal_proposal(closed.storage, intent=_intent(cycle, spec))
    graph = closed.world.setup.graph
    tenant = closed.world.tenant_id
    other = _outsider(closed)
    with tenant_context(tenant):
        ClientAssignment.objects.create(
            tenant=graph.tenant, client=graph.client, user=other
        )
    before = _state(tenant, row)
    outcome, statements = _attempt(
        tenant,
        lambda: propose(
            other,
            cycle.pk,
            ProposalDraft(
                spec,
                timezone.now() + timedelta(hours=72),
                previous_id=row.pk,
                previous_version=row.version,
            ),
            expected_version=cycle.version,
        ),
    )
    assert isinstance(outcome, PermissionDenied), outcome
    _no_dml(statements)
    assert _state(tenant, row) == before


def _waiver_blob(world: StorageWorld, change: str) -> dict[str, object]:
    data: dict[str, object] = dict(waiver_inputs(world).as_data())
    if change == "incomplete-fiscal":
        return {"action_kind": "fiscal_manifestation", "step_key": "ciencia"}
    if change == "unknown-discriminator":
        data["action_kind"] = "retired_effect"
    elif change == "missing-discriminator":
        del data["action_kind"]
    elif change == "non-text-discriminator":
        data["action_kind"] = 7
    elif change == "truncated-body":
        del data["body"]
    return data


STORED_INPUTS = [
    "incomplete-fiscal",
    "unknown-discriminator",
    "missing-discriminator",
    "non-text-discriminator",
    "truncated-body",
]


@pytest.mark.parametrize("change", STORED_INPUTS)
@pytest.mark.parametrize("command", ["approve", "revalidate-system"])
def test_a_stored_input_that_contradicts_its_row_is_an_integrity_refusal(
    closed: Closed, change: str, command: str
) -> None:
    row = fiscal_proposal(
        closed.storage,
        blob=_waiver_blob(closed.storage, change),
        kind=ActionKind.REQUEST_WAIVER,
    )
    graph = closed.world.setup.graph
    tenant = closed.world.tenant_id
    before = _state(tenant, row)
    call = (
        (lambda: approve(graph.actor, row.pk, expected_version=row.version))
        if command == "approve"
        else (lambda: revalidate(None, row.pk))
    )
    outcome, statements = _attempt(tenant, call)
    assert isinstance(outcome, cycle_errors.ProposalIntegrityError), outcome
    _no_dml(statements)
    assert _state(tenant, row) == before


def _unknown_column(closed: Closed) -> ActionProposal:
    """A frozen row whose column and bytes both name a kind this code does not know.

    The manifest builder cannot serialize an unknown kind, so the bytes are a valid
    waiver manifest with only its kind rewritten (the frozen trigger binds the two).
    """
    graph = closed.world.setup.graph
    spec = waiver_inputs(closed.storage)
    with tenant_context(closed.world.tenant_id):
        cycle = ServiceCycle.objects.get(pk=closed.world.setup.cycle.pk)
        pk = uuid7()
        expires = (timezone.now() + timedelta(hours=72)).replace(microsecond=0)
        key = _intent(cycle, spec)
        vector = replace(
            _manifest(),
            action_kind=ActionKind.REQUEST_WAIVER,
            tenant_id=str(graph.tenant.pk),
            client_id=str(graph.client.pk),
            cycle_id=str(cycle.pk),
            proposal_id=str(pk),
            proposal_revision=1,
            initiator_id=str(graph.staff.pk),
            business_intent_key=key,
            not_before=None,
            expires_at=expires,
        )
        data = json.loads(vector.canonical_bytes())
        data["action_kind"] = "retired_effect"
        body = canonicalize(data)
        return ActionProposal.objects.create(
            pk=pk,
            tenant=graph.tenant,
            client=graph.client,
            cycle=cycle,
            initiator=graph.staff,
            business_intent_key=key,
            action_kind="retired_effect",
            input_spec={**spec.as_data(), "action_kind": "retired_effect"},
            manifest_bytes=body,
            digest=hashlib.sha256(body).hexdigest(),
            expires_at=expires,
        )


def test_an_unknown_stored_column_is_an_integrity_refusal(closed: Closed) -> None:
    row = _unknown_column(closed)
    graph = closed.world.setup.graph
    tenant = closed.world.tenant_id
    before = _state(tenant, row)
    outcome, statements = _attempt(
        tenant, lambda: approve(graph.actor, row.pk, expected_version=row.version)
    )
    assert isinstance(outcome, cycle_errors.ProposalIntegrityError), outcome
    _no_dml(statements)
    response = closed_session(closed).get(reverse("approval-card", args=[row.pk]))
    assert response.status_code == 422
    assert _state(tenant, row) == before


@pytest.mark.parametrize("predecessor", ["truncated-body", "conflict"])
@pytest.mark.parametrize("method", ["get", "post"])
def test_the_revision_route_refuses_a_broken_or_conflicted_predecessor(
    closed: Closed, predecessor: str, method: str
) -> None:
    if predecessor == "conflict":
        row = legacy_proposal(closed.storage)
        marker(row, business_intent_key=OTHER_INTENT)
    else:
        row = fiscal_proposal(
            closed.storage,
            blob=_waiver_blob(closed.storage, "truncated-body"),
            kind=ActionKind.REQUEST_WAIVER,
        )
    cycle = closed.cycle
    tenant = closed.world.tenant_id
    before = _state(tenant, row)
    url = reverse("proposal-create", args=[cycle.pk]) + f"?revisao_de={row.pk}"
    client = closed_session(closed)
    response = (
        client.get(url)
        if method == "get"
        else client.post(url, {"expected_version": cycle.version})
    )
    assert response.status_code == 422
    assert _state(tenant, row) == before


# Section 7.6: completion and delivery are bound to their operation.


@pytest.mark.parametrize("operation", ["apply", "freeze"])
def test_completion_refuses_a_genuinely_exact_receipt_of_another_operation(
    operation: str,
) -> None:
    setup = completion_setup(f"bound-{operation}")
    graph = setup.graph
    with tenant_context(graph.tenant.pk):
        contact = ClientContact.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            name="Synthetic recipient",
            email="recipient@example.test",
        )
        cycle = ServiceCycle.objects.get(pk=setup.cycle.pk)
        notice = propose(
            graph.staff,
            cycle.pk,
            ProposalDraft(
                ProposalInput(
                    action_kind=ActionKind.EMAIL_NOTICE,
                    step_key="bound-notice",
                    recipient_ids=(contact.pk,),
                    subject="Aviso",
                    body="Aviso aprovado.",
                ),
                timezone.now() + timedelta(days=1),
            ),
            expected_version=cycle.version,
        )
        notice = approve(
            graph.actor, notice.pk, expected_version=notice.version
        ).proposal
    marker(notice)
    with tenant_context(graph.tenant.pk):
        cycle = ServiceCycle.objects.get(pk=setup.cycle.pk)
        capture_index(graph.actor, cycle.pk, expected_version=cycle.version)
        cycle.refresh_from_db()

        def counts() -> tuple[object, ...]:
            row = ServiceCycle.objects.get(pk=cycle.pk)
            return (
                row.state,
                row.version,
                CompletionGeneration.objects.count(),
                EvidenceIndex.objects.count(),
            )

        before = counts()
        outcome: object = None
        try:
            with transaction.atomic():
                if operation == "apply":
                    apply_completion(graph.actor, notice)
                else:
                    freeze_completion(graph.actor, notice)
        except cycle_errors.IllegalTransition as error:
            outcome = error
        after = counts()
    assert isinstance(outcome, cycle_errors.ProposalIntegrityError), outcome
    assert after == before


CROSSED = {
    "notice-as-publication": "effect_kind_mismatch",
    # Revalidating this proposal would invalidate it: the operation check comes first.
    "stale-notice-as-publication": "effect_kind_mismatch",
    "fiscal-proposal": "effect_kind_mismatch",
    "malformed-proposal": "proposal_integrity",
    "mixed-history": "execution_conflict",
}


def _second_notice(setup: ApprovalGraph) -> ActionProposal:
    graph = setup.graph
    cycle = ServiceCycle.objects.get(pk=setup.cycle.pk)
    contact = ClientContact.objects.create(
        tenant=graph.tenant,
        client=graph.client,
        name="Second recipient",
        email="second@example.test",
    )
    second = propose(
        graph.staff,
        cycle.pk,
        ProposalDraft(
            ProposalInput(
                action_kind=ActionKind.EMAIL_NOTICE,
                step_key="notice-two",
                recipient_ids=(contact.pk,),
                subject="Segundo aviso",
                body="Segundo aviso aprovado.",
            ),
            timezone.now() + timedelta(days=1),
        ),
        expected_version=cycle.version,
    )
    second = approve(graph.actor, second.pk, expected_version=second.version).proposal
    return execute(graph.actor, second.pk, expected_version=second.version).proposal


def _email_blob(setup: ApprovalGraph) -> dict[str, object]:
    data: dict[str, object] = dict(
        ProposalInput(
            action_kind=ActionKind.EMAIL_NOTICE,
            step_key="notice-broken",
            recipient_ids=(setup.contact.pk,),
            subject="Aviso",
            body="Corpo.",
        ).as_data()
    )
    del data["body"]
    return data


@pytest.mark.parametrize("case", list(CROSSED))
def test_a_crossed_or_broken_claim_is_cancelled_before_start_and_the_next_is_sent(
    settings: SettingsWrapper, case: str
) -> None:
    settings.DELIVERY_PAUSED = False
    setup = reviewable(f"crossed-{case}")
    graph = setup.graph
    tenant = graph.tenant.pk
    with tenant_context(tenant):
        first = execute(
            graph.actor, setup.proposal.pk, expected_version=setup.proposal.version
        ).proposal
        bad = OutboundMessage.objects.get(proposal_id=first.pk)
        good = OutboundMessage.objects.get(proposal_id=_second_notice(setup).pk)
    if case in {"notice-as-publication", "stale-notice-as-publication"}:
        changes: dict[str, object] = {"kind": "approved_publication_notice"}
        if case.startswith("stale"):
            with tenant_context(tenant):
                ClientContact.objects.filter(pk=setup.contact.pk).update(
                    email="changed-recipient@example.test"
                )
    elif case == "fiscal-proposal":
        changes = {"proposal_id": stored_row(graph, setup.cycle, RowShape()).pk}
    elif case == "malformed-proposal":
        broken = stored_row(
            graph,
            setup.cycle,
            RowShape(kind=ActionKind.EMAIL_NOTICE, blob=_email_blob(setup)),
            ProposalState.APPROVED,
        )
        changes = {"proposal_id": broken.pk}
    else:
        marker(first, business_intent_key=OTHER_INTENT)
        changes = {}
    with tenant_context(tenant):
        OutboundMessage.objects.filter(pk=bad.pk).update(
            next_attempt_at=good.next_attempt_at - timedelta(seconds=1), **changes
        )
    before = _state(tenant, first)[:3]
    mail.outbox.clear()
    crashed: object = None
    try:
        dispatch_tenant(str(tenant))
    except Exception as error:  # noqa: BLE001 - a claim must never escape the batch
        crashed = error
    assert crashed is None, crashed
    with tenant_context(tenant):
        refused = OutboundMessage.objects.get(pk=bad.pk)
        sent = OutboundMessage.objects.get(pk=good.pk)
        started = CycleEvent.objects.filter(
            type="proposal.dispatch_started", payload__message_id=str(bad.pk)
        ).exists()
    assert (refused.state, refused.last_error_code) == (
        MessageState.CANCELLED,
        CROSSED[case],
    )
    assert refused.send_started_at is None
    assert started is False
    assert sent.send_started_at is not None
    assert len(mail.outbox) == 1
    assert _state(tenant, first)[:3] == before


# Section 7.7: the locked waiver proof is batched.


def _blocked(cycle: ServiceCycle) -> set[str]:
    return {item.key for item in completion_items(cycle) if item.code == "requests"}


def test_the_locked_waiver_proof_takes_the_same_queries_for_one_or_many() -> None:
    counts: dict[int, int] = {}
    for count in (1, 3):
        world = portal_setup(f"waiver-scale-{count}")
        graph = world.graph
        with tenant_context(graph.tenant.pk):
            ClientAssignment.objects.create(
                tenant=graph.tenant, client=graph.client, user=graph.staff
            )
            proven: set[str] = set()
            for index in range(count):
                world.cycle.refresh_from_db()
                request = create_request(
                    graph.actor,
                    world.cycle.pk,
                    RequestFields(f"Waived request {index}"),
                    expected_version=world.cycle.version,
                )
                proposal = propose_waiver(
                    graph.staff,
                    request.pk,
                    reason=f"Not applicable: {index}.",
                    expected_version=request.version,
                )
                proposal = approve(
                    graph.actor, proposal.pk, expected_version=proposal.version
                ).proposal
                assert execute(
                    graph.actor, proposal.pk, expected_version=proposal.version
                ).executed
                request.refresh_from_db()
                proven.add(f"requests:{request.pk}:{request.version}")
            world.cycle.refresh_from_db()
            bare = create_request(
                graph.actor,
                world.cycle.pk,
                RequestFields("Waived without an approval"),
                expected_version=world.cycle.version,
            )
            waive_request(
                graph.actor,
                bare.pk,
                reason="Sem aprovação.",
                expected_version=bare.version,
            )
            bare.refresh_from_db()
            assert bare.state == RequestState.WAIVED
            cycle = ServiceCycle.objects.get(pk=world.cycle.pk)
            blocked = _blocked(cycle)
            assert f"requests:{bare.pk}:{bare.version}" in blocked
            assert not proven & blocked
            with CaptureQueriesContext(connection) as queries:
                completion_items(cycle)
            counts[count] = len(queries.captured_queries)
            assert (
                EvidenceRequest.objects.filter(
                    cycle=cycle, state=RequestState.WAIVED
                ).count()
                == count + 1
            )
    assert counts[1] == counts[3], counts
