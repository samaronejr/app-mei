"""Cycle-owned briefing sources, all narrowed before their SQL projection."""

from django.db.models import CharField, DateField, Exists, F, OuterRef, Subquery, Value
from django.db.models.functions import Cast, Coalesce, Concat

from apps.briefing.source_rows import SourceContext, SourceQuery, project
from apps.cycles.evidence import CLIENT_OPEN_STATES, counted_links
from apps.cycles.models import (
    ActionProposal,
    CycleEvent,
    EvidenceRequest,
    ExceptionInternal,
    ProposalState,
)
from apps.cycles.models import Exception as CycleException
from apps.cycles.services.exceptions import ACTIVE_STATES


def requests(context: SourceContext) -> SourceQuery:
    """Only unanswered, overdue requests still need a client response."""
    submitted = counted_links().filter(request_id=OuterRef("pk"))
    latest_event = (
        CycleEvent.objects.filter(
            client_id=OuterRef("client_id"),
            aggregate_type="EvidenceRequest",
            aggregate_id=OuterRef("pk"),
            type__in=("evidence.submitted", "document.reviewed"),
        )
        .order_by("-pk")
        .values("pk")[:1]
    )
    rows = EvidenceRequest.objects.filter(
        client_id__in=context.clients.values("pk"),
        state__in=CLIENT_OPEN_STATES,
        due_on__lt=context.today,
    ).filter(~Exists(submitted))
    return project(
        rows.annotate(
            brief_kind=Value("request"),
            brief_period=F("cycle__period_start"),
            brief_owner=F("cycle__owner_id"),
            brief_owner_name=Coalesce("cycle__owner__email", Value("")),
            brief_due=F("due_on"),
            brief_version=Concat(
                Cast("version", CharField()),
                Value(":"),
                Coalesce(Cast(Subquery(latest_event), CharField()), Value("")),
            ),
            brief_observed=F("requested_at"),
            brief_target=F("cycle_id"),
            brief_title=F("title"),
            brief_action=Value("Revisar solicitação"),
            brief_service=F("cycle__engagement__service_kind"),
            brief_state=F("state"),
            brief_visible=Value(value=False),
        )
    )


def exceptions(context: SourceContext) -> SourceQuery:
    """Keep private instructions private to the same authorized firm portfolio."""
    instructions = ExceptionInternal.objects.filter(
        exception_id=OuterRef("pk"), client_id=OuterRef("client_id")
    ).values("next_action")[:1]
    return project(
        CycleException.objects.filter(
            client_id__in=context.clients.values("pk"), state__in=ACTIVE_STATES
        ).annotate(
            brief_kind=Value("exception"),
            brief_period=F("cycle__period_start"),
            brief_owner=F("owner_id"),
            brief_owner_name=F("owner__email"),
            brief_due=Coalesce("due_on", "review_on"),
            brief_version=Cast("version", CharField()),
            brief_observed=F("created_at"),
            brief_target=F("cycle_id"),
            brief_title=F("reason_code"),
            brief_action=Coalesce(Subquery(instructions), Value("Revisar pendência")),
            brief_service=F("cycle__engagement__service_kind"),
            brief_state=F("state"),
            brief_visible=F("client_visible"),
        )
    )


def approvals(context: SourceContext) -> SourceQuery:
    """Awaiting is an approval queue state; approved is not an execution receipt."""
    return project(
        ActionProposal.objects.filter(
            client_id__in=context.clients.values("pk"), state=ProposalState.AWAITING
        ).annotate(
            brief_kind=Value("approval"),
            brief_period=F("cycle__period_start"),
            brief_owner=Coalesce("cycle__reviewer_id", "cycle__owner_id"),
            brief_owner_name=Coalesce(
                "cycle__reviewer__email", "cycle__owner__email", Value("")
            ),
            brief_due=Cast("expires_at", DateField()),
            brief_version=Cast("version", CharField()),
            brief_observed=F("created_at"),
            brief_target=F("pk"),
            brief_title=Value("Aprovação aguardando decisão"),
            brief_action=Value("Revisar efeitos propostos"),
            brief_service=F("cycle__engagement__service_kind"),
            brief_state=F("state"),
            brief_visible=Value(value=False),
        )
    )
