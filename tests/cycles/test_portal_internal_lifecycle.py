"""Ordinary request operations cannot change an internal request's audience."""

from __future__ import annotations

from uuid import uuid4

import pytest
from django.db import DatabaseError, connection, transaction
from psycopg.errors import CheckViolation

from apps.clients.models import ClientAssignment
from apps.core.tenancy import tenant_context
from apps.cycles.bulk import BulkItemState, BulkKind
from apps.cycles.bulk_contracts import Selection
from apps.cycles.effects.executors import execute
from apps.cycles.errors import IllegalTransition
from apps.cycles.evidence import (
    DocumentIntake,
    EvidenceRequest,
    IntakeState,
    RequestState,
    ReviewDecision,
    ScanVerdict,
)
from apps.cycles.models import CycleState
from apps.cycles.services import requests
from apps.cycles.services.approvals import approve
from apps.cycles.services.bulk import preview_bulk
from apps.cycles.services.completion import propose_reopen
from apps.cycles.services.cycle import transition_cycle
from apps.cycles.services.proposed_transitions import propose_waiver
from apps.cycles.services.review import record_review
from apps.cycles.tasks import run_bulk_operation
from apps.tenants.models import TenantRole
from tests.cycles.bulk_fixtures import confirm, item_states
from tests.cycles.completion_fixtures import close_cycle, completion_setup
from tests.portal.request_fixtures import (
    assert_hidden_link,
    internal_request_link,
    portal_setup,
)
from tests.ui.factories import add_member

pytestmark = pytest.mark.django_db(transaction=True)


def test_edit_notice_and_ordinary_waiver_do_not_admit_an_internal_request() -> None:
    setup = portal_setup("internal-request-writes")
    graph, cycle = setup.graph, setup.cycle
    link = internal_request_link(graph, cycle)
    fields = requests.RequestFields(title="Ordinary", client_note="Needed for filing.")
    with tenant_context(graph.tenant.pk):
        internal = EvidenceRequest.objects.get(pk=link.request_id)
        with pytest.raises(IllegalTransition):
            requests.update_request(
                graph.actor, internal.pk, fields, expected_version=internal.version
            )
    assert_hidden_link(link, setup.request.pk)
    with tenant_context(graph.tenant.pk), pytest.raises(IllegalTransition):
        requests.waive_request(
            graph.actor,
            internal.pk,
            reason="Dispensa.",
            expected_version=internal.version,
        )
    assert_hidden_link(link, setup.request.pk)
    with tenant_context(graph.tenant.pk), pytest.raises(IllegalTransition):
        requests.remind_request(
            graph.actor, internal.pk, expected_version=internal.version
        )
    assert_hidden_link(link, setup.request.pk)
    with tenant_context(graph.tenant.pk):
        cycle.refresh_from_db()
        draft = requests.create_request(
            graph.actor, cycle.pk, fields, expected_version=cycle.version
        )
        cycle.refresh_from_db()
        requests.send_requests(graph.actor, cycle.pk, expected_version=cycle.version)
        draft.refresh_from_db()
        assert draft.state == RequestState.REQUESTED
    assert_hidden_link(link, draft.pk)
    with tenant_context(graph.tenant.pk):
        ClientAssignment.objects.create(
            tenant=graph.tenant, client=graph.client, user=graph.staff
        )
        waiver = propose_waiver(
            graph.staff,
            draft.pk,
            reason="Não é mais necessário.",
            expected_version=draft.version,
        )
        approved = approve(graph.actor, waiver.pk, expected_version=waiver.version)
        assert execute(
            graph.actor, waiver.pk, expected_version=approved.proposal.version
        ).executed
        draft.refresh_from_db()
        assert draft.state == RequestState.WAIVED
    assert_hidden_link(link, draft.pk)


def test_completion_reopening_and_reassignment_keep_internal_evidence_hidden() -> None:
    setup = completion_setup("internal-request-close-reopen")
    graph, cycle = setup.graph, setup.cycle
    link = internal_request_link(graph, cycle)
    with tenant_context(graph.tenant.pk):
        close_cycle(setup)
        cycle.refresh_from_db()
        assert cycle.state == CycleState.COMPLETED
    assert_hidden_link(link)
    independent = add_member(
        graph.tenant, "reopen@internal.test", TenantRole.OPERATIONS_ADMIN
    )
    with tenant_context(graph.tenant.pk):
        proposal = propose_reopen(
            graph.actor,
            cycle.pk,
            reason="Nova evidência fiscal no período.",
            affected_scope=(link.request_id,),
            expected_version=cycle.version,
        )
        approved = approve(independent, proposal.pk, expected_version=proposal.version)
        assert execute(
            independent, proposal.pk, expected_version=approved.proposal.version
        ).executed
        cycle.refresh_from_db()
        assert cycle.state == CycleState.ACTIVE
    assert_hidden_link(link)
    # Requests have no assignee. A change to the owning cycle is an admin/ORM
    # edit, not a request state transition.
    with tenant_context(graph.tenant.pk):
        cycle.owner = graph.staff
        cycle.save(update_fields=["owner"])
        cycle.refresh_from_db()
        assert cycle.owner_id == graph.staff.pk
    assert_hidden_link(link)


def test_cycle_cancellation_keeps_internal_evidence_hidden() -> None:
    setup = portal_setup("internal-request-cancel")
    graph, cycle = setup.graph, setup.cycle
    link = internal_request_link(graph, cycle)
    with tenant_context(graph.tenant.pk):
        cycle.refresh_from_db()
        transition_cycle(
            graph.actor,
            cycle.pk,
            CycleState.CANCELLED,
            reason="Cancelamento do trabalho.",
            expected_version=cycle.version,
        )
        cycle.refresh_from_db()
        assert cycle.state == CycleState.CANCELLED
    assert_hidden_link(link, setup.request.pk)


@pytest.mark.parametrize("decision", [ReviewDecision.ACCEPTED, ReviewDecision.REJECTED])
def test_review_only_changes_the_internal_links_decision(decision: str) -> None:
    setup = portal_setup(f"internal-review-{decision}")
    graph = setup.graph
    link = internal_request_link(graph, setup.cycle)
    with tenant_context(graph.tenant.pk):
        DocumentIntake.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            document_id=link.document_id,
            state=IntakeState.CLASSIFIED,
            verdict=ScanVerdict.CLEAN,
        )
        record_review(
            graph.actor,
            link.pk,
            decision=decision,
            client_reason="Arquivo conferido.",
            corrections=[],
            expected_version=link.request.version,
        )
        link.refresh_from_db()
        assert link.review_decision == decision
        assert link.request.state == RequestState.INTERNAL
    assert_hidden_link(link, setup.request.pk)


def test_bulk_notice_only_sends_the_draft_request() -> None:
    setup = portal_setup("internal-request-bulk")
    graph = setup.graph
    graph.tenant.features = ["cycles_r1", "bulk_r1"]
    graph.tenant.save(update_fields=["features"])
    link = internal_request_link(graph, setup.cycle)
    with tenant_context(graph.tenant.pk):
        draft = requests.create_request(
            graph.actor,
            setup.cycle.pk,
            requests.RequestFields(title="Document"),
            expected_version=setup.cycle.version,
        )
        preview = preview_bulk(
            graph.actor, BulkKind.SEND_REQUEST_NOTICES, Selection((setup.cycle.pk,))
        )
    operation = confirm(graph.actor, preview)
    run_bulk_operation(str(operation.tenant_id), str(operation.pk))
    with tenant_context(graph.tenant.pk):
        assert set(item_states(operation).values()) == {BulkItemState.DONE}
        draft.refresh_from_db()
        assert draft.state == RequestState.REQUESTED
    assert_hidden_link(link, setup.request.pk)


def test_admin_reparent_and_delete_cannot_release_retained_internal_evidence() -> None:
    setup = portal_setup("internal-request-reparent")
    link = internal_request_link(setup.graph, setup.cycle)
    # With no purpose, the state change reaches the lifetime trigger itself.
    states = {*RequestState.values, "future_state"} - {RequestState.INTERNAL}
    cases = (
        *(
            (
                "UPDATE cycles_evidencerequest SET state = %s WHERE id = %s",
                [state, link.request_id],
                "evidencerequest_internal_lifetime",
            )
            for state in sorted(states)
        ),
        (
            "UPDATE cycles_evidencelink SET request_id = %s WHERE id = %s",
            [setup.request.pk, link.pk],
            "evidencelink_internal_lifetime",
        ),
        (
            "UPDATE cycles_evidencelink SET id = %s WHERE id = %s",
            [uuid4(), link.pk],
            "evidencelink_internal_lifetime",
        ),
        (
            "DELETE FROM cycles_evidencerequest WHERE id = %s",
            [link.request_id],
            "evidencerequest_internal_lifetime",
        ),
    )
    for sql, params, invariant in cases:
        with (
            tenant_context(link.tenant_id),
            connection.cursor() as cursor,
            pytest.raises(DatabaseError) as refused,
            transaction.atomic(),
        ):
            cursor.execute(sql, params)
        cause = refused.value.__cause__
        assert isinstance(cause, CheckViolation)
        assert cause.diag.constraint_name == invariant
        assert_hidden_link(link, setup.request.pk)
