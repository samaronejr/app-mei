"""Calendar, outbox and intake facts projected without external side effects."""

from datetime import timedelta

from django.db.models import CharField, DateField, F, OuterRef, Q, Subquery, Value
from django.db.models.functions import Cast, Coalesce, Concat

from apps.briefing.source_rows import SourceContext, SourceQuery, project
from apps.clients.models import AssignmentRole, ClientAssignment, Engagement
from apps.clients.models.engagement import ServiceKind
from apps.cycles.models import DocumentIntake, EvidenceLink, IntakeState
from apps.delivery.models import MessageState, OutboundMessage
from apps.obligations.queries import DUE_SOON_HORIZON_DAYS, unsettled_for


def obligations(context: SourceContext) -> SourceQuery:
    """Use the calendar queue's unsettled policy, including already overdue rows."""
    responsible = (
        Engagement.objects.filter(
            client_id=OuterRef("client_id"),
            starts_on__lte=OuterRef("competence_month"),
            service_kind=ServiceKind.MEI_MENSAL,
        )
        .filter(Q(ends_on__isnull=True) | Q(ends_on__gt=OuterRef("competence_month")))
        .order_by("-starts_on", "pk")
    )
    assigned = ClientAssignment.objects.filter(
        client_id=OuterRef("client_id"), role=AssignmentRole.PRIMARY
    ).order_by("pk")
    return project(
        unsettled_for(context.clients)
        .filter(
            resolved_due_date__lte=context.today + timedelta(days=DUE_SOON_HORIZON_DAYS)
        )
        .annotate(
            brief_kind=Value("obligation"),
            brief_period=F("competence_month"),
            brief_owner=Coalesce(
                Subquery(responsible.values("responsible_id")[:1]),
                Subquery(assigned.values("user_id")[:1]),
            ),
            brief_owner_name=Coalesce(
                Subquery(responsible.values("responsible__email")[:1]),
                Subquery(assigned.values("user__email")[:1]),
                Value(""),
            ),
            brief_due=F("resolved_due_date"),
            # This legacy table has a timestamp revision rather than `version`.
            # Include its schedule/state so update_fields cannot hide a changed fact.
            brief_version=Concat(
                Cast("updated_at", CharField()),
                Value(":"),
                F("status"),
                Value(":"),
                Cast("resolved_due_date", CharField()),
            ),
            brief_observed=F("updated_at"),
            brief_target=F("client_id"),
            brief_title=F("obligation_type_id"),
            brief_action=Value("Revisar obrigação"),
            brief_service=Value(ServiceKind.MEI_MENSAL),
            brief_state=F("status"),
            brief_visible=Value(value=False),
        )
    )


def deliveries(context: SourceContext) -> SourceQuery:
    """Surface refused or uncertain sends for a person, never an automatic resend."""
    return project(
        OutboundMessage.objects.filter(
            client_id__in=context.clients.values("pk"),
            state__in=(MessageState.UNKNOWN, MessageState.REJECTED),
        ).annotate(
            brief_kind=Value("delivery"),
            brief_period=F("cycle__period_start"),
            brief_owner=Coalesce("cycle__owner_id", "created_by_id"),
            brief_owner_name=Coalesce("cycle__owner__email", "created_by__email"),
            brief_due=Value(None, output_field=DateField()),
            brief_version=Cast("version", CharField()),
            brief_observed=F("updated_at"),
            brief_target=F("pk"),
            brief_title=Value("Envio exige revisão"),
            brief_action=Value("Conferir o resultado antes de reenviar"),
            brief_service=Coalesce("cycle__engagement__service_kind", Value("")),
            brief_state=F("state"),
            brief_visible=Value(value=False),
        )
    )


def documents(context: SourceContext) -> SourceQuery:
    """Project one divergent fact even when the document has several evidence links."""
    links = EvidenceLink.objects.filter(
        document_id=OuterRef("document_id"), client_id=OuterRef("client_id")
    ).order_by("pk")
    return project(
        DocumentIntake.objects.filter(
            client_id__in=context.clients.values("pk"), state=IntakeState.DIVERGENT
        ).annotate(
            brief_kind=Value("document"),
            brief_period=Subquery(links.values("request__cycle__period_start")[:1]),
            brief_owner=Subquery(links.values("request__cycle__owner_id")[:1]),
            brief_owner_name=Coalesce(
                Subquery(links.values("request__cycle__owner__email")[:1]), Value("")
            ),
            brief_due=Value(None, output_field=DateField()),
            brief_version=Cast("version", CharField()),
            brief_observed=Coalesce("scanned_at", "created_at"),
            brief_target=Subquery(links.values("pk")[:1]),
            brief_title=Value("Documento de outra empresa"),
            brief_action=Value("Revisar vínculo e documento"),
            brief_service=Coalesce(
                Subquery(links.values("request__cycle__engagement__service_kind")[:1]),
                Value(""),
            ),
            brief_state=F("state"),
            brief_visible=Value(value=False),
        )
    )
