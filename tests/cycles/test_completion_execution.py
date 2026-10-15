"""The exception is the only missing guard in the mutation control."""

from datetime import date, timedelta

import pytest
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.cycles.completion_records import CompletionGeneration
from apps.cycles.effects.executors import execute
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.errors import IllegalTransition
from apps.cycles.models import (
    ActionExecution,
    CycleEvent,
    CycleState,
    ExceptionState,
    ServiceCycle,
)
from apps.cycles.models import Exception as CycleException
from apps.cycles.services.approvals import propose
from tests.cycles.completion_fixtures import (
    approved_completion,
    close_cycle,
    completion_setup,
)

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("state", [ExceptionState.OPEN, ExceptionState.HANDLING])
def test_only_open_blocking_exception_prevents_approved_completion(
    state: str,
) -> None:
    setup = completion_setup("completion-open-exception")
    with tenant_context(setup.graph.tenant.pk):
        CycleException.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            cycle=setup.cycle,
            subject_ref=setup.cycle.pk,
            reason_code="completion_review",
            owner=setup.graph.actor,
            review_on=date(2026, 9, 30),
            blocks_completion=True,
            state=state,
        )
        proposal = approved_completion(setup)
        before = CycleEvent.objects.count()
        with pytest.raises(IllegalTransition):
            execute(setup.graph.actor, proposal.pk, expected_version=proposal.version)
        assert ServiceCycle.objects.get(pk=setup.cycle.pk).state == CycleState.EXECUTION
        assert not ActionExecution.objects.filter(proposal=proposal).exists()
        assert not CompletionGeneration.objects.filter(cycle=setup.cycle).exists()
        assert CycleEvent.objects.count() == before


@pytest.mark.parametrize(
    ("state", "blocking"),
    [
        (ExceptionState.OPEN, False),
        (ExceptionState.RESOLVED, True),
        (ExceptionState.RISK_ACCEPTED, True),
        (ExceptionState.CANCELLED, True),
    ],
)
def test_nonblocking_or_terminal_exception_allows_approved_completion(
    state: str, blocking: bool
) -> None:
    setup = completion_setup("completion-exception-control")
    with tenant_context(setup.graph.tenant.pk):
        CycleException.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            cycle=setup.cycle,
            subject_ref=setup.cycle.pk,
            reason_code="completion_review",
            owner=setup.graph.actor,
            review_on=date(2026, 9, 30),
            blocks_completion=blocking,
            state=state,
        )
        proposal_id = close_cycle(setup)
        generation = CompletionGeneration.objects.get(cycle=setup.cycle)
        assert generation.proposal_id == proposal_id
        assert generation.amendment == 0
        assert ServiceCycle.objects.get(pk=setup.cycle.pk).state == CycleState.COMPLETED
        assert ActionExecution.objects.filter(proposal_id=proposal_id).count() == 1


def test_missing_generated_index_rolls_back_exact_execution_receipt() -> None:
    setup = completion_setup("completion-index-required")
    with tenant_context(setup.graph.tenant.pk):
        proposal = approved_completion(setup, capture=False)
        with pytest.raises(IllegalTransition):
            execute(setup.graph.actor, proposal.pk, expected_version=proposal.version)
        assert not ActionExecution.objects.filter(proposal=proposal).exists()
        assert not CompletionGeneration.objects.filter(cycle=setup.cycle).exists()


def test_another_completion_action_is_not_exempted_by_the_executors_own_receipt() -> (
    None
):
    setup = completion_setup("completion-other-action")
    with tenant_context(setup.graph.tenant.pk):
        other = propose(
            setup.graph.staff,
            setup.cycle.pk,
            ProposalDraft(
                ProposalInput(
                    action_kind=ActionKind.CYCLE_COMPLETION, step_key="other-completion"
                ),
                timezone.now() + timedelta(days=1),
            ),
            expected_version=setup.cycle.version,
        )
        proposal = approved_completion(setup)
        with pytest.raises(IllegalTransition):
            execute(setup.graph.actor, proposal.pk, expected_version=proposal.version)
        assert not ActionExecution.objects.filter(
            proposal__in=[other, proposal]
        ).exists()
        assert not CompletionGeneration.objects.filter(cycle=setup.cycle).exists()
