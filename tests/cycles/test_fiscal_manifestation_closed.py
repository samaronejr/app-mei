"""Until the builder lands, no fiscal Ciencia proposal can be created, decided or run.

AXRM-022-SCAFFOLD: rewritten in 22-06 (no fiscal effect registered), deleted in 22-14.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, cast

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.authz.models import Role
from apps.core.identifiers import uuid7
from apps.core.tenancy import tenant_context
from apps.cycles import errors as cycle_errors
from apps.cycles.bulk import (
    BulkItem,
    BulkItemState,
    BulkKind,
    BulkOperation,
    BulkOperationState,
)
from apps.cycles.bulk_contracts import Selection, digest
from apps.cycles.effects.executors import execute
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.models import (
    ActionExecution,
    ActionProposal,
    Approval,
    CycleEvent,
    EvidenceRequest,
    ProposalState,
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
from apps.cycles.services.completion import propose_reopen
from apps.cycles.services.cycle import open_cycle
from apps.cycles.services.proposed_transitions import propose_waiver
from apps.cycles.tasks import run_bulk_operation
from apps.tenants.models import Membership
from tests.browser.harness import mfa_session_cookie
from tests.cycles.bulk_fixtures import confirm
from tests.cycles.completion_fixtures import close_cycle, completion_setup
from tests.integrations.ciencia_fixtures import manifestation_kind
from tests.integrations.ciencia_storage_rows import (
    RowShape,
    StorageWorld,
    fiscal_proposal,
    legacy_proposal,
    storage_world,
    stored_row,
    waiver_inputs,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from apps.obligations.models import FiscalDocument
    from tests.integrations.capture_fixtures import CaptureWorld

pytestmark = pytest.mark.django_db(transaction=True)
REFUSAL = "fiscal_manifestation_unavailable"
WRITES = ("INSERT", "UPDATE", "DELETE")


@dataclass
class Closed:
    """A proposal-ready graph: open cycle, assigned initiator, one received NF-e."""

    storage: StorageWorld

    @property
    def world(self) -> CaptureWorld:
        return self.storage.capture

    @property
    def document(self) -> FiscalDocument:
        return self.storage.document

    @property
    def cycle(self) -> ServiceCycle:
        with tenant_context(self.world.tenant_id):
            return ServiceCycle.objects.get(pk=self.world.setup.cycle.pk)

    def inputs(self, kind: object, *, documents: int = 1) -> ProposalInput:
        return ProposalInput(
            action_kind=cast("ActionKind", kind),
            step_key="ciencia",
            fiscal_document_ids=(self.document.pk,) * documents,
            provider_link_id=self.world.link.pk if documents else None,
        )

    def stored(self, state: str) -> ActionProposal:
        return fiscal_proposal(self.storage, state)

    def storage_inputs(self) -> ProposalInput:
        return self.inputs(manifestation_kind())

    def counts(self) -> tuple[list[UUID], int, int, int, int]:
        with tenant_context(self.world.tenant_id):
            return (
                sorted(ActionProposal.objects.values_list("pk", flat=True)),
                CycleEvent.objects.count(),
                Approval.objects.count(),
                ActionExecution.objects.count(),
                self.cycle.version,
            )


@pytest.fixture
def closed(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Closed:
    return Closed(storage_world(settings, tmp_path, monkeypatch))


def _attempt(closed: Closed, call: Callable[[], object]) -> tuple[object, list[str]]:
    """Run the real command; capture its outcome and every statement it issued."""
    outcome: object
    with (
        tenant_context(closed.world.tenant_id),
        CaptureQueriesContext(connection) as queries,
    ):
        try:
            outcome = call()
        except Exception as error:  # noqa: BLE001 - the outcome is the assertion
            outcome = error
    return outcome, [query["sql"] for query in queries.captured_queries]


def _refused_without_writes(outcome: object, statements: list[str]) -> None:
    assert isinstance(outcome, ValidationError), outcome
    assert outcome.code == REFUSAL, outcome
    written = [sql for sql in statements if sql.lstrip().upper().startswith(WRITES)]
    assert written == [], written


@pytest.mark.parametrize(
    ("form", "documents"),
    [("enum", 1), ("enum", 2), ("string", 0)],
    ids=["one-document", "same-document-twice", "bare-string-no-selection"],
)
def test_propose_refuses_every_fiscal_selection_without_writing(
    closed: Closed, form: str, documents: int
) -> None:
    # Resolved here, not at collection, so a parent without the kind fails on its
    # contract (manifestation_kind's assertion) rather than at import.
    kind: object = manifestation_kind() if form == "enum" else "fiscal_manifestation"
    before = closed.counts()
    cycle = closed.cycle
    outcome, statements = _attempt(
        closed,
        lambda: propose(
            closed.world.setup.graph.staff,
            cycle.pk,
            ProposalDraft(
                closed.inputs(kind, documents=documents),
                expires_at=timezone.now() + timedelta(hours=72),
            ),
            expected_version=cycle.version,
        ),
    )
    _refused_without_writes(outcome, statements)
    assert closed.counts() == before


def test_propose_refuses_a_revision_of_a_stored_fiscal_row(closed: Closed) -> None:
    prior = closed.stored(ProposalState.AWAITING)
    before = closed.counts()
    cycle = closed.cycle
    outcome, statements = _attempt(
        closed,
        lambda: propose(
            closed.world.setup.graph.staff,
            cycle.pk,
            ProposalDraft(
                closed.inputs(manifestation_kind()),
                expires_at=timezone.now() + timedelta(hours=72),
                previous_id=prior.pk,
                previous_version=prior.version,
            ),
            expected_version=cycle.version,
        ),
    )
    _refused_without_writes(outcome, statements)
    assert closed.counts() == before
    with tenant_context(closed.world.tenant_id):
        kept = ActionProposal.objects.get(pk=prior.pk)
    assert (kept.state, kept.version, bytes(kept.manifest_bytes)) == (
        prior.state,
        prior.version,
        bytes(prior.manifest_bytes),
    )


def test_a_legacy_proposal_on_the_same_graph_still_succeeds(closed: Closed) -> None:
    request = closed.world.setup.request
    with tenant_context(closed.world.tenant_id):
        proposal = propose_waiver(
            closed.world.setup.graph.staff,
            request.pk,
            reason="Synthetic reason",
            expected_version=request.version,
        )
    assert proposal.action_kind == ActionKind.REQUEST_WAIVER
    assert proposal.state == ProposalState.AWAITING


# Section 3: a stored row is refused from its STORED kind, whatever its blob decodes to.

BLOBS = ["fiscal-blob", "decodable-waiver-blob"]
COMMANDS = [
    "approve",
    "reject",
    "cancel",
    "revalidate-actor",
    "revalidate-system",
    "execute",
]


def _row(closed: Closed, blob: str, state: str) -> ActionProposal:
    data = waiver_inputs(closed.storage).as_data() if blob != "fiscal-blob" else None
    return fiscal_proposal(closed.storage, state, blob=data)


def _snapshot(closed: Closed, row: ActionProposal) -> tuple[object, ...]:
    with tenant_context(closed.world.tenant_id):
        kept = ActionProposal.objects.get(pk=row.pk)
        request = EvidenceRequest.objects.get(pk=closed.world.setup.request.pk)
        return (
            kept.state,
            kept.version,
            bytes(kept.manifest_bytes),
            kept.input_spec,
            kept.invalidation_reasons,
            CycleException.objects.count(),
            (request.state, request.version),
            closed.counts(),
        )


@pytest.mark.parametrize("blob", BLOBS)
@pytest.mark.parametrize("command", COMMANDS)
def test_a_stored_fiscal_row_is_refused_from_its_stored_kind(
    closed: Closed, command: str, blob: str
) -> None:
    state = ProposalState.APPROVED if command == "execute" else ProposalState.AWAITING
    row = _row(closed, blob, state)
    graph = closed.world.setup.graph
    before = _snapshot(closed, row)
    calls: dict[str, Callable[[], object]] = {
        "approve": lambda: approve(graph.actor, row.pk, expected_version=row.version),
        "reject": lambda: reject(
            graph.actor, row.pk, expected_version=row.version, reason="Motivo."
        ),
        "cancel": lambda: cancel(graph.staff, row.pk, expected_version=row.version),
        "revalidate-actor": lambda: revalidate(graph.actor, row.pk),
        "revalidate-system": lambda: revalidate(None, row.pk),
        "execute": lambda: execute(graph.staff, row.pk, expected_version=row.version),
    }
    outcome, statements = _attempt(closed, calls[command])
    _refused_without_writes(outcome, statements)
    assert _snapshot(closed, row) == before


def test_a_legacy_row_whose_blob_names_another_legacy_kind_is_an_integrity_refusal(
    closed: Closed,
) -> None:
    refusal = getattr(cycle_errors, "ProposalIntegrityError", None)
    assert refusal is not None, "no typed integrity refusal"
    contact = closed.world.setup.graph.client.pk
    email = ProposalInput(
        action_kind=ActionKind.EMAIL_NOTICE,
        step_key="notice",
        recipient_ids=(contact,),
        subject="Assunto",
        body="Corpo.",
    ).as_data()
    row = fiscal_proposal(closed.storage, blob=email, kind=ActionKind.REQUEST_WAIVER)
    before = _snapshot(closed, row)
    graph = closed.world.setup.graph
    outcome, statements = _attempt(
        closed, lambda: approve(graph.actor, row.pk, expected_version=row.version)
    )
    assert isinstance(outcome, refusal), outcome
    assert issubclass(refusal, cycle_errors.IllegalTransition)
    written = [sql for sql in statements if sql.lstrip().upper().startswith(WRITES)]
    assert written == [], written
    assert _snapshot(closed, row) == before


def test_a_legacy_draft_cannot_supersede_a_fiscal_row_under_its_intent(
    closed: Closed,
) -> None:
    spec = waiver_inputs(closed.storage)
    cycle = closed.cycle
    row = fiscal_proposal(closed.storage, intent=_intent(cycle, spec))
    before = _snapshot(closed, row)
    outcome, statements = _attempt(
        closed,
        lambda: propose(
            closed.world.setup.graph.staff,
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
    _refused_without_writes(outcome, statements)
    assert _snapshot(closed, row) == before


def test_a_reopening_with_a_pending_fiscal_sibling_is_refused_without_writing() -> None:
    setup = completion_setup("fiscal-sibling-reopen")
    graph = setup.graph
    with tenant_context(graph.tenant.pk):
        close_cycle(setup)
    sibling = stored_row(graph, setup.cycle, RowShape())
    with tenant_context(graph.tenant.pk):
        cycle = ServiceCycle.objects.get(pk=setup.cycle.pk)
        reviewer = User.objects.create_user(email="fiscal-reopen@example.test")
        Membership.objects.create(
            tenant=graph.tenant, user=reviewer, role=Role.OPERATIONS_ADMIN
        )
        reopening = propose_reopen(
            graph.actor,
            cycle.pk,
            reason="Conferir uma ciência pendente.",
            affected_scope=(cycle.pk,),
            expected_version=cycle.version,
        )
        decided = approve(reviewer, reopening.pk, expected_version=reopening.version)
        cycle.refresh_from_db()
    outcome: object
    with tenant_context(graph.tenant.pk):
        try:
            outcome = execute(
                reviewer, reopening.pk, expected_version=decided.proposal.version
            )
        except ValidationError as error:
            outcome = error
        after = ServiceCycle.objects.get(pk=cycle.pk)
        kept = ActionProposal.objects.get(pk=sibling.pk)
        markers = ActionExecution.objects.filter(proposal=reopening).count()
    assert isinstance(outcome, ValidationError), outcome
    assert outcome.code == REFUSAL
    assert (after.state, after.amendment, after.version) == (
        cycle.state,
        cycle.amendment,
        cycle.version,
    )
    assert (kept.state, kept.version) == (sibling.state, sibling.version)
    assert markers == 0


def _session(closed: Closed) -> Client:
    # capture_world's ingest setup already enrolled the actor's authenticator.
    graph = closed.world.setup.graph
    host = f"{graph.tenant.slug}.localhost"
    client = Client(SERVER_NAME=host, raise_request_exception=False)
    cookie = mfa_session_cookie(graph.actor, f"http://{host}/")
    client.cookies[cookie["name"]] = cookie["value"]
    return client


@pytest.mark.parametrize("state", [ProposalState.AWAITING, ProposalState.APPROVED])
def test_a_stored_fiscal_card_is_read_only_history(closed: Closed, state: str) -> None:
    row = _row(closed, "decodable-waiver-blob", state)
    response = _session(closed).get(reverse("approval-card", args=[row.pk]))
    assert response.status_code == 200
    page = response.content.decode()
    assert 'name="action"' not in page
    assert "revisao_de=" not in page
    for flag in ("may_execute", "may_approve", "may_cancel", "may_propose"):
        assert response.context[flag] is False, flag
    assert response.context["awaiting"] is False
    assert response.context["open"] is False
    assert response.context.get("effect_unavailable") is True


@pytest.mark.parametrize("blob", BLOBS)
@pytest.mark.parametrize(
    "action", ["approve", "reject", "cancel", "revalidate", "execute"]
)
def test_a_forged_decision_on_a_stored_fiscal_card_is_refused(
    closed: Closed, action: str, blob: str
) -> None:
    state = ProposalState.APPROVED if action == "execute" else ProposalState.AWAITING
    row = _row(closed, blob, state)
    before = _snapshot(closed, row)
    response = _session(closed).post(
        reverse("approval-card", args=[row.pk]),
        {"action": action, "expected_version": row.version, "reason": "Motivo."},
    )
    assert response.status_code == 422
    assert _snapshot(closed, row) == before


@pytest.mark.parametrize("method", ["get", "post"])
def test_the_revision_route_refuses_a_stored_fiscal_predecessor(
    closed: Closed, method: str
) -> None:
    row = _row(closed, "fiscal-blob", ProposalState.AWAITING)
    cycle = closed.cycle
    before = _snapshot(closed, row)
    url = reverse("proposal-create", args=[cycle.pk]) + f"?revisao_de={row.pk}"
    client = _session(closed)
    response = (
        client.get(url)
        if method == "get"
        else client.post(url, {"expected_version": cycle.version})
    )
    assert response.status_code == 422
    assert reverse("cycle-detail", args=[cycle.pk]) in response.content.decode()
    assert _snapshot(closed, row) == before


def test_the_revision_route_keeps_404_for_another_cycle(closed: Closed) -> None:
    row = _row(closed, "fiscal-blob", ProposalState.AWAITING)
    graph = closed.world.setup.graph
    with tenant_context(closed.world.tenant_id):
        other = open_cycle(
            graph.actor,
            graph.engagement.pk,
            closed.cycle.period_start.replace(
                month=closed.cycle.period_start.month + 1
            ),
            graph.definition.pk,
            idempotency_key="fiscal-revision-other-cycle",
        )
    url = reverse("proposal-create", args=[other.id]) + f"?revisao_de={row.pk}"
    assert _session(closed).get(url).status_code == 404


def _bulk(closed: Closed) -> tuple[ActionProposal, ActionProposal]:
    tenant = closed.world.setup.graph.tenant
    tenant.features = [*tenant.features, "bulk_r1"]
    tenant.save(update_fields=["features"])
    legacy = legacy_proposal(closed.storage)
    fiscal = _row(closed, "decodable-waiver-blob", ProposalState.AWAITING)
    return legacy, fiscal


def _preview(closed: Closed, *subjects: ActionProposal) -> BulkOperation:
    with tenant_context(closed.world.tenant_id):
        return preview_bulk(
            closed.world.setup.graph.actor,
            BulkKind.APPROVE_ITEMS,
            Selection(tuple(row.pk for row in subjects)),
        )


def _as_eligible_before_this_part(
    operation: BulkOperation, subject: UUID
) -> BulkOperation:
    """Insert the preview a run before 22-04 would have frozen (fiscal row eligible).

    A preview's identity is immutable, so the older preview is its own frozen row.
    """
    pk = uuid7()
    snapshot = {
        **operation.snapshot_json,
        "operation_id": str(pk),
        "items": [
            {**row, "eligible": True, "reason": ""}
            if row["subject_ref"] == str(subject)
            else row
            for row in operation.snapshot_json["items"]
        ],
    }
    with tenant_context(operation.tenant_id):
        older = BulkOperation(
            pk=pk,
            tenant_id=operation.tenant_id,
            requested_by_id=operation.requested_by_id,
            kind=operation.kind,
            created_at=operation.created_at,
            valid_until=operation.valid_until,
            snapshot_json=snapshot,
            snapshot_digest=digest(snapshot),
            idempotency_key=digest([str(operation.tenant_id), str(pk)]),
        )
        older.save(force_insert=True)
        return BulkOperation.objects.get(pk=pk)


def test_a_bulk_preview_marks_a_stored_fiscal_row_ineligible(closed: Closed) -> None:
    legacy, fiscal = _bulk(closed)
    operation = _preview(closed, legacy, fiscal)
    items = {row["subject_ref"]: row for row in operation.snapshot_json["items"]}
    assert (items[str(legacy.pk)]["eligible"], items[str(legacy.pk)]["reason"]) == (
        True,
        "",
    )
    assert (items[str(fiscal.pk)]["eligible"], items[str(fiscal.pk)]["reason"]) == (
        False,
        "effect_unavailable",
    )


def test_confirming_an_older_preview_that_marked_it_eligible_is_refused(
    closed: Closed,
) -> None:
    legacy, fiscal = _bulk(closed)
    stale = _as_eligible_before_this_part(_preview(closed, legacy, fiscal), fiscal.pk)
    outcome: object
    try:
        outcome = confirm(closed.world.setup.graph.actor, stale)
    except (cycle_errors.VersionConflict, ValidationError) as error:
        outcome = error
    assert isinstance(outcome, cycle_errors.VersionConflict), outcome
    with tenant_context(closed.world.tenant_id):
        assert not BulkItem.objects.filter(operation_id=stale.pk).exists()


def _queued_before_this_part(operation: BulkOperation) -> None:
    """Queue every eligible frozen item exactly as run_bulk did before 22-04."""
    with tenant_context(operation.tenant_id):
        for position, row in enumerate(operation.snapshot_json["items"]):
            eligible = bool(row["eligible"])
            BulkItem.objects.create(
                tenant_id=operation.tenant_id,
                client_id=row["client_id"],
                operation=operation,
                subject_ref=row["subject_ref"],
                expected_version=row["expected_version"],
                effect_key=digest(
                    [
                        str(operation.tenant_id),
                        str(row["client_id"]),
                        str(operation.pk),
                        str(row["subject_ref"]),
                    ]
                ),
                position=position,
                state=BulkItemState.PENDING
                if eligible
                else BulkItemState.SKIPPED_INELIGIBLE,
                result_json={} if eligible else {"code": row["reason"]},
                finished_at=None if eligible else timezone.now(),
            )
        BulkOperation.objects.filter(pk=operation.pk).update(
            state=BulkOperationState.QUEUED,
            confirmed_at=timezone.now(),
            version=operation.version + 1,
        )


def test_an_already_queued_fiscal_item_is_refused_with_only_its_receipt(
    closed: Closed,
) -> None:
    legacy, fiscal = _bulk(closed)
    operation = _as_eligible_before_this_part(
        _preview(closed, legacy, fiscal), fiscal.pk
    )
    _queued_before_this_part(operation)
    before = _snapshot(closed, fiscal)
    run_bulk_operation(str(closed.world.tenant_id), str(operation.pk))
    with tenant_context(closed.world.tenant_id):
        items = {
            row.subject_ref: row for row in BulkItem.objects.filter(operation=operation)
        }
        approvals = set(Approval.objects.values_list("proposal_id", flat=True))
    refused = items[str(fiscal.pk)]
    assert refused.state == BulkItemState.FAILED
    assert refused.result_json.get("code") == "execution_refused"
    assert fiscal.pk not in approvals
    assert items[str(legacy.pk)].state == BulkItemState.DONE
    after = _snapshot(closed, fiscal)
    assert after[:5] == before[:5]


def test_the_cycle_detail_labels_a_stored_fiscal_row_by_its_kind(
    closed: Closed,
) -> None:
    data = {**closed.storage_inputs().as_data(), "reopen": True}
    row = fiscal_proposal(closed.storage, blob=data)
    page = _session(closed).get(reverse("cycle-detail", args=[closed.cycle.pk]))
    assert page.status_code == 200
    text = page.content.decode()
    assert reverse("approval-card", args=[row.pk]) in text
    assert "Reabrir ciclo" not in text
