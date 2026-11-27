"""An execution marker is exact, absent or conflicting (AXRM-022, deliberate, kept).

The exact foreign key binds a marker to its proposal, revision and digest only. A marker
proves its proposal's effect only when all eight fields agree: tenant, client, cycle,
proposal, revision, digest, business intent and kind. Any other marker attached to the
proposal, or occupying its scoped intent, is a conflict: neither proof of execution nor
permission to run the effect again. No marker is ever deleted or rewritten here.
"""

from enum import StrEnum
from typing import Final

from django.db.models import Exists, F, OuterRef, Q, QuerySet

from apps.cycles.models import ActionExecution, ActionProposal

EXACT: Final = Q(
    tenant_id=F("proposal__tenant_id"),
    client_id=F("proposal__client_id"),
    cycle_id=F("proposal__cycle_id"),
    proposal_revision=F("proposal__revision"),
    digest=F("proposal__digest"),
    business_intent_key=F("proposal__business_intent_key"),
    action_kind=F("proposal__action_kind"),
)


class MarkerState(StrEnum):
    """What the markers around one proposal prove."""

    ABSENT = "absent"
    EXACT = "exact"
    CONFLICT = "conflict"


def exact_markers() -> QuerySet[ActionExecution]:
    """Markers whose eight fields agree with their own proposal (one join)."""
    return ActionExecution.objects.filter(EXACT)


def exactly_executed() -> Exists:
    """For an ActionProposal queryset: the outer row has an exact marker."""
    return Exists(exact_markers().filter(proposal_id=OuterRef("pk")))


def marker_state(
    proposal: ActionProposal,
) -> tuple[MarkerState, ActionExecution | None]:
    """Classify every marker attached to the proposal or occupying its intent."""
    candidates = list(
        ActionExecution.objects.filter(
            Q(proposal_id=proposal.pk)
            | Q(
                client_id=proposal.client_id,
                business_intent_key=proposal.business_intent_key,
            )
        ).order_by("pk")
    )
    if not candidates:
        return MarkerState.ABSENT, None
    receipt = candidates[0]
    exact = (
        len(candidates) == 1
        and exact_markers().filter(pk=receipt.pk, proposal_id=proposal.pk).exists()
    )
    return (MarkerState.EXACT, receipt) if exact else (MarkerState.CONFLICT, None)
