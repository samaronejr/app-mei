"""Cycle guards for the consequential effects and their unresolved outbound work."""

from apps.cycles.effects.manifest import ActionKind
from apps.cycles.models import (
    ActionExecution,
    ActionProposal,
    ProposalState,
    ServiceCycle,
)
from apps.delivery.models import MessageState, OutboundMessage


def actions_terminal(cycle: ServiceCycle) -> bool:
    """Require every proposal to be decided/executed and every send resolved."""
    undecided = (
        ActionProposal.objects.filter(cycle=cycle)
        .exclude(
            state__in=[
                ProposalState.REJECTED,
                ProposalState.CANCELLED,
                ProposalState.INVALIDATED,
            ]
        )
        .filter(executions__isnull=True)
        .exists()
    )
    unresolved = OutboundMessage.objects.filter(
        cycle=cycle,
        state__in=[
            MessageState.QUEUED,
            MessageState.SENDING,
            MessageState.TEMPORARY,
            MessageState.UNKNOWN,
        ],
    ).exists()
    return not undecided and not unresolved


def effects_started(cycle: ServiceCycle) -> bool:
    """Treat an applied DB effect or durable SMTP start as irreversible history."""
    return (
        ActionExecution.objects.filter(cycle=cycle)
        .exclude(action_kind=ActionKind.EMAIL_NOTICE)
        .exists()
        or OutboundMessage.objects.filter(
            cycle=cycle, proposal_id__isnull=False, send_started_at__isnull=False
        ).exists()
    )
