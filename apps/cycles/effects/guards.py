"""Cycle guards for the consequential effects and their unresolved outbound work."""

from apps.cycles.effects.manifest import ActionKind
from apps.cycles.effects.markers import MarkerState, with_marker_state
from apps.cycles.models import (
    ActionProposal,
    ProposalState,
    ServiceCycle,
)
from apps.delivery.models import MessageState, OutboundMessage


def actions_terminal(cycle: ServiceCycle) -> bool:
    """Require every proposal to be decided/executed and every send resolved."""
    undecided = (
        with_marker_state(ActionProposal.objects.filter(cycle=cycle))
        .exclude(
            state__in=[
                ProposalState.REJECTED,
                ProposalState.CANCELLED,
                ProposalState.INVALIDATED,
            ]
        )
        .exclude(marker_state=MarkerState.EXACT)
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
    """Return whether a proved non-email effect or an SMTP start exists (ruling 3).

    Mixed or conflicting history alone proves nothing: it does not block cancellation.
    """
    return (
        with_marker_state(ActionProposal.objects.filter(cycle=cycle))
        .filter(marker_state=MarkerState.EXACT)
        .exclude(action_kind=ActionKind.EMAIL_NOTICE)
        .exists()
        or OutboundMessage.objects.filter(
            cycle=cycle, proposal_id__isnull=False, send_started_at__isnull=False
        ).exists()
    )
