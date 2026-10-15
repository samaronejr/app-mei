"""Small real approval graphs shared by authority and race scenarios."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from threading import Event
from typing import Final

from django.utils import timezone

from apps.clients.models import ClientAssignment, ClientContact
from apps.core.tenancy import tenant_context
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.models import ActionProposal, ServiceCycle
from apps.cycles.services.approvals import propose
from apps.cycles.services.cycle import open_cycle
from tests.cycles.cycle_fixtures import PERIOD, CycleSetup, cycle_setup

SIGNAL_TIMEOUT: Final = 15


@dataclass
class LockSignal:
    """Observe a real locking SELECT without replacing its query or result."""

    table: str
    entered: Event
    release: Event | None = None
    after: bool = False

    def _signal(self) -> None:
        self.entered.set()
        if self.release is not None:
            assert self.release.wait(SIGNAL_TIMEOUT), "The SQL gate was not released."

    def __call__(
        self,
        execute: Callable[..., object],
        sql: str,
        params: object,
        many: bool,
        context: dict[str, object],
    ) -> object:
        """Forward exactly once, signaling only the named actual lock statement."""
        selected = f'FROM "{self.table}"' in sql and (
            "FOR NO KEY UPDATE" in sql or "FOR UPDATE" in sql
        )
        if selected and not self.after:
            self._signal()
        result = execute(sql, params, many, context)
        if selected and self.after:
            self._signal()
        return result


@dataclass(frozen=True, slots=True)
class ApprovalGraph:
    """The two firm actors and exact stored proposal used in a scenario."""

    graph: CycleSetup
    cycle: ServiceCycle
    contact: ClientContact
    proposal: ActionProposal


def approval_graph(
    slug: str = "approval-authority",
    *,
    body: str = "Confira os documentos selecionados.",
) -> ApprovalGraph:
    """Create an assigned staff author and an independent owner approver."""
    graph = cycle_setup(slug)
    with tenant_context(graph.tenant.pk):
        ClientAssignment.objects.create(
            tenant=graph.tenant, client=graph.client, user=graph.staff
        )
        contact = ClientContact.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            name="Synthetic recipient",
            email="recipient@example.test",
        )
        ref = open_cycle(
            graph.actor,
            graph.engagement.pk,
            PERIOD,
            graph.definition.pk,
            idempotency_key=slug,
        )
        cycle = ServiceCycle.objects.get(pk=ref.id)
        proposal = propose(
            graph.staff,
            cycle.pk,
            ProposalDraft(
                ProposalInput(
                    action_kind=ActionKind.EMAIL_NOTICE,
                    step_key="notice",
                    recipient_ids=(contact.pk,),
                    subject="Documentos de setembro",
                    body=body,
                ),
                timezone.now() + timedelta(days=1),
            ),
            expected_version=cycle.version,
        )
        cycle.refresh_from_db()
    return ApprovalGraph(graph, cycle, contact, proposal)
