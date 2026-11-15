"""Captured fiscal heads awaiting their own revision's accountant decision."""

from django.db.models import (
    Case,
    CharField,
    DateField,
    Exists,
    F,
    OuterRef,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Cast, Coalesce, Concat, TruncMonth

from apps.briefing.source_rows import SourceContext, SourceQuery, project
from apps.clients.models import AssignmentRole, ClientAssignment
from apps.core.flags import tenant_has
from apps.cycles.fiscal_records import FiscalDocumentReview
from apps.cycles.services.cycle import tenant_id
from apps.obligations.models import FiscalDocument


def fiscal_documents(context: SourceContext) -> SourceQuery:
    """Project a private source in the existing scoped briefing UNION."""
    reviewed = FiscalDocumentReview.objects.filter(
        revision_id=OuterRef("current_revision_id")
    )
    assigned = ClientAssignment.objects.filter(
        client_id=OuterRef("client_id"), role=AssignmentRole.PRIMARY
    ).order_by("pk")
    rows = FiscalDocument.objects.filter(
        client_id__in=context.clients.values("pk"),
        source="captura",
        current_revision__isnull=False,
    ).filter(~Exists(reviewed))
    if not tenant_has(tenant_id(), "document_capture"):
        rows = rows.none()
    return project(
        rows.annotate(
            brief_kind=Value("fiscal_document"),
            brief_period=Case(
                When(
                    family="nfe",
                    then=Cast(TruncMonth("issue_date"), DateField()),
                ),
                default=F("competence"),
                output_field=DateField(),
            ),
            brief_owner=Subquery(assigned.values("user_id")[:1]),
            brief_owner_name=Coalesce(
                Subquery(assigned.values("user__email")[:1]), Value("")
            ),
            brief_due=Value(None, output_field=DateField()),
            brief_version=Concat(
                Cast("version", CharField()),
                Value(":"),
                Cast("current_revision_id", CharField()),
            ),
            brief_observed=F("current_revision__observed_at"),
            brief_target=F("pk"),
            brief_title=Value("Documento capturado aguardando revisão"),
            brief_action=Value("Conferir a versão e os arquivos retidos"),
            brief_service=Value(""),
            brief_state=Value("aguardando_revisao"),
            brief_visible=Value(value=False),
        )
    )
