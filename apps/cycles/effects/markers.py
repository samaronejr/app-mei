"""One proposal-relative classification of execution markers (AXRM-022, kept).

The exact foreign key binds a marker to its proposal, revision and digest only, so every
marker is compared with the TARGET proposal on all eight fields: tenant, client, cycle,
proposal, revision, digest, business intent and kind. Reviewer ruling 1 fixes the
order:

- CONFLICT: a marker attached to the proposal disagrees with it (even beside an exact
  one);
- EXACT: a marker attached to the proposal agrees with it;
- OCCUPIED: none is attached, but a marker holds its (tenant, client, business intent);
- ABSENT: otherwise.

Only EXACT proves an effect, and only for the proposal's own kind. Readers use this one
SQL expression; no marker is ever deleted or rewritten here.
"""

from enum import StrEnum
from typing import TYPE_CHECKING, Final, TypedDict

from django.db.models import Case, CharField, Exists, OuterRef, Q, Value, When

from apps.cycles.models import ActionExecution, ActionProposal

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django_stubs_ext import WithAnnotations


class MarkerState(StrEnum):
    """What the markers around one proposal prove (reviewer ruling 1)."""

    ABSENT = "absent"
    EXACT = "exact"
    CONFLICT = "conflict"
    OCCUPIED = "occupied"


class MarkerFacts(TypedDict):
    """The annotation `with_marker_state` adds to each proposal row."""

    marker_state: str


# A marker equals the OUTER (target) proposal on all eight fields.
MATCHES_TARGET: Final = Q(
    tenant_id=OuterRef("tenant_id"),
    client_id=OuterRef("client_id"),
    cycle_id=OuterRef("cycle_id"),
    proposal_id=OuterRef("pk"),
    proposal_revision=OuterRef("revision"),
    digest=OuterRef("digest"),
    business_intent_key=OuterRef("business_intent_key"),
    action_kind=OuterRef("action_kind"),
)


def marker_state_expression() -> Case:
    """For an ActionProposal queryset: classify each row against itself, in SQL."""
    attached = ActionExecution.objects.filter(proposal_id=OuterRef("pk"))
    holders = ActionExecution.objects.filter(
        tenant_id=OuterRef("tenant_id"),
        client_id=OuterRef("client_id"),
        business_intent_key=OuterRef("business_intent_key"),
    )
    return Case(
        When(
            Exists(attached.exclude(MATCHES_TARGET)), then=Value(MarkerState.CONFLICT)
        ),
        When(Exists(attached.filter(MATCHES_TARGET)), then=Value(MarkerState.EXACT)),
        When(Exists(holders), then=Value(MarkerState.OCCUPIED)),
        default=Value(MarkerState.ABSENT),
        output_field=CharField(),
    )


def with_marker_state(
    queryset: "QuerySet[ActionProposal]",
) -> "QuerySet[WithAnnotations[ActionProposal, MarkerFacts]]":
    """Annotate `marker_state` on every proposal row, in the same single query."""
    return queryset.annotate(marker_state=marker_state_expression())


def marker_state(
    proposal: ActionProposal,
) -> tuple[MarkerState, ActionExecution | None]:
    """Classify one proposal with the same expression; a receipt only for EXACT."""
    value = (
        with_marker_state(ActionProposal.objects.filter(pk=proposal.pk))
        .values_list("marker_state", flat=True)
        .first()
    )
    state = MarkerState(value) if value is not None else MarkerState.ABSENT
    if state is not MarkerState.EXACT:
        return state, None
    return state, ActionExecution.objects.filter(proposal_id=proposal.pk).first()


def proves(proposal: ActionProposal, kind: str) -> bool:
    """EXACT and of this kind: the only state that proves the proposal's effect."""
    return (
        proposal.action_kind == kind and marker_state(proposal)[0] is MarkerState.EXACT
    )


def proving(kind: str) -> "QuerySet[WithAnnotations[ActionProposal, MarkerFacts]]":
    """Proposals of this kind whose state is EXACT (one correlated expression)."""
    return with_marker_state(ActionProposal.objects.filter(action_kind=kind)).filter(
        marker_state=MarkerState.EXACT
    )
