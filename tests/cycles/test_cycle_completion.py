"""Completion is a conjunction of real evidence and exact approval authority."""

import pytest
from django.utils import timezone

from apps.clients.models import ClientAssignment
from apps.core.tenancy import tenant_context
from apps.cycles.effects.executors import execute
from apps.cycles.errors import IllegalTransition
from apps.cycles.evidence_index import capture_index
from apps.cycles.models import (
    ActionExecution,
    CycleEvent,
    CycleState,
    CycleTask,
    ExceptionState,
    RequestInternal,
    RequestState,
    ServiceCycle,
    TaskState,
)
from apps.cycles.models import Exception as CycleException
from apps.cycles.services.approvals import approve
from apps.cycles.services.cycle import (
    COMPLETION_GUARDS,
    completion_unmet,
    open_cycle,
    transition_cycle,
)
from apps.cycles.services.proposed_transitions import propose_waiver
from apps.cycles.services.requests import RequestFields, create_request
from tests.cycles.cycle_fixtures import PERIOD, CycleSetup, cycle_setup

pytestmark = pytest.mark.django_db


def ready_cycle(setup: CycleSetup) -> ServiceCycle:
    ref = open_cycle(
        setup.actor,
        setup.engagement.pk,
        PERIOD,
        setup.definition.pk,
        idempotency_key="completion",
    )
    cycle = ServiceCycle.objects.get(pk=ref.id)
    CycleTask.objects.filter(cycle=cycle).update(state=TaskState.ACCEPTED)
    ServiceCycle.objects.filter(pk=cycle.pk).update(state=CycleState.EXECUTION)
    cycle.refresh_from_db()
    return cycle


def test_required_request_is_a_completion_condition() -> None:
    setup = cycle_setup("completion-required")
    with tenant_context(setup.tenant.pk):
        cycle = ready_cycle(setup)
        create_request(
            setup.actor,
            cycle.pk,
            RequestFields("Documento obrigatório"),
            expected_version=cycle.version,
        )
        assert "requests" in completion_unmet(cycle)


def test_unapproved_waiver_does_not_satisfy_required_request() -> None:
    setup = cycle_setup("completion-waiver")
    with tenant_context(setup.tenant.pk):
        cycle = ready_cycle(setup)
        request = create_request(
            setup.actor,
            cycle.pk,
            RequestFields("Documento dispensado sem aprovação"),
            expected_version=cycle.version,
        )
        request.state = RequestState.WAIVED
        request.save(update_fields=["state"])
        RequestInternal.objects.filter(request=request).update(
            waiver_reason="Não é prova de uma aprovação."
        )
        assert "requests" in completion_unmet(cycle)


def test_optional_request_does_not_block_required_request_condition() -> None:
    setup = cycle_setup("completion-optional")
    with tenant_context(setup.tenant.pk):
        cycle = ready_cycle(setup)
        create_request(
            setup.actor,
            cycle.pk,
            RequestFields("Documento opcional", required=False),
            expected_version=cycle.version,
        )
        assert "requests" not in completion_unmet(cycle)


def test_direct_transition_cannot_replace_exact_completion_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = cycle_setup("completion-authority")
    with tenant_context(setup.tenant.pk):
        cycle = ready_cycle(setup)
        # Isolate authority from the independent index condition.
        monkeypatch.setitem(COMPLETION_GUARDS, "evidence_index", lambda _: True)
        before = CycleEvent.objects.count()
        with pytest.raises(IllegalTransition):
            transition_cycle(
                setup.actor,
                cycle.pk,
                CycleState.COMPLETED,
                expected_version=cycle.version,
            )
        cycle.refresh_from_db()
        assert cycle.state == CycleState.EXECUTION
        assert CycleEvent.objects.count() == before
        assert not ActionExecution.objects.exists()


@pytest.mark.parametrize(
    ("state", "blocking", "unmet"),
    [
        (ExceptionState.OPEN, True, True),
        (ExceptionState.HANDLING, True, True),
        (ExceptionState.OPEN, False, False),
        (ExceptionState.RESOLVED, True, False),
        (ExceptionState.RISK_ACCEPTED, True, False),
        (ExceptionState.CANCELLED, True, False),
    ],
)
def test_exception_condition_has_nonblocking_and_terminal_controls(
    state: str,
    blocking: bool,
    unmet: bool,
) -> None:
    setup = cycle_setup("completion-exception")
    with tenant_context(setup.tenant.pk):
        cycle = ready_cycle(setup)
        CycleException.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            cycle=cycle,
            subject_ref=cycle.pk,
            reason_code="completion_review",
            owner=setup.actor,
            review_on=timezone.localdate(),
            blocks_completion=blocking,
            state=state,
        )
        assert ("exceptions" in completion_unmet(cycle)) is unmet


def test_executed_exact_waiver_satisfies_the_required_request() -> None:
    setup = cycle_setup("completion-approved-waiver")
    with tenant_context(setup.tenant.pk):
        ClientAssignment.objects.create(
            tenant=setup.tenant, client=setup.client, user=setup.staff
        )
        cycle = ready_cycle(setup)
        request = create_request(
            setup.actor,
            cycle.pk,
            RequestFields("Documento dispensável"),
            expected_version=cycle.version,
        )
        proposal = propose_waiver(
            setup.staff,
            request.pk,
            reason="Documento não aplicável ao período.",
            expected_version=request.version,
        )
        proposal = approve(
            setup.actor, proposal.pk, expected_version=proposal.version
        ).proposal
        assert execute(
            setup.actor, proposal.pk, expected_version=proposal.version
        ).executed
        request.refresh_from_db()
        assert request.state == RequestState.WAIVED
        assert "requests" not in completion_unmet(cycle)
        assert "actions" not in completion_unmet(cycle)


def test_unexecuted_waiver_is_an_action_condition_even_for_optional_request() -> None:
    setup = cycle_setup("completion-open-action")
    with tenant_context(setup.tenant.pk):
        cycle = ready_cycle(setup)
        request = create_request(
            setup.actor,
            cycle.pk,
            RequestFields("Opcional", required=False),
            expected_version=cycle.version,
        )
        propose_waiver(
            setup.actor,
            request.pk,
            reason="Não aplicável.",
            expected_version=request.version,
        )
        assert "requests" not in completion_unmet(cycle)
        assert "actions" in completion_unmet(cycle)


def test_unaccepted_task_and_stale_index_are_separate_conditions() -> None:
    setup = cycle_setup("completion-task-index")
    with tenant_context(setup.tenant.pk):
        cycle = ready_cycle(setup)
        capture_index(setup.actor, cycle.pk, expected_version=cycle.version)
        cycle.refresh_from_db()
        assert completion_unmet(cycle) == []
        task = CycleTask.objects.filter(cycle=cycle).first()
        assert task is not None
        task.state = TaskState.READY
        task.version += 1
        task.save(update_fields=["state", "version"])
        unmet = completion_unmet(cycle)
        assert "tasks" in unmet
        assert "evidence_index" in unmet
