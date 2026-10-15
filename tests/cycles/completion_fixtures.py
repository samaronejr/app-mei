"""Real author, independent approver and generated evidence for closure tests."""

from dataclasses import dataclass
from uuid import UUID

from apps.clients.models import ClientAssignment
from apps.core.tenancy import tenant_context
from apps.cycles.effects.executors import execute
from apps.cycles.evidence_index import capture_index
from apps.cycles.models import ActionProposal, ServiceCycle
from apps.cycles.services.approvals import approve
from apps.cycles.services.proposed_transitions import propose_completion
from tests.cycles.cycle_fixtures import CycleSetup, cycle_setup
from tests.cycles.test_cycle_completion import ready_cycle


@dataclass(frozen=True, slots=True)
class CompletionSetup:
    graph: CycleSetup
    cycle: ServiceCycle


def completion_setup(slug: str) -> CompletionSetup:
    graph = cycle_setup(slug)
    with tenant_context(graph.tenant.pk):
        ClientAssignment.objects.create(
            tenant=graph.tenant, client=graph.client, user=graph.staff
        )
        cycle = ready_cycle(graph)
    return CompletionSetup(graph, cycle)


def approved_completion(
    setup: CompletionSetup,
    *,
    state: str = "concluido",
    reasons: dict[str, str] | None = None,
    capture: bool = True,
) -> ActionProposal:
    cycle = ServiceCycle.objects.get(pk=setup.cycle.pk)
    if capture:
        capture_index(setup.graph.actor, cycle.pk, expected_version=cycle.version)
        cycle.refresh_from_db()
    proposal = propose_completion(
        setup.graph.staff,
        cycle.pk,
        to_state=state,
        reason=reasons if reasons is not None else "",
        expected_version=cycle.version,
    )
    return approve(
        setup.graph.actor, proposal.pk, expected_version=proposal.version
    ).proposal


def close_cycle(setup: CompletionSetup) -> UUID:
    proposal = approved_completion(setup)
    result = execute(setup.graph.actor, proposal.pk, expected_version=proposal.version)
    assert result.executed
    return proposal.pk
