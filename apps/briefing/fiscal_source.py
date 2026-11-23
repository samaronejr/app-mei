"""Captured fiscal heads awaiting their own revision's accountant decision."""

from django.db.models import (
    Case,
    DateField,
    F,
    OuterRef,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Cast, Coalesce, JSONArray, TruncMonth

from apps.briefing.source_rows import SourceContext, SourceQuery, project
from apps.clients.models import AssignmentRole, ClientAssignment
from apps.core.flags import tenant_has
from apps.cycles.fiscal_material import sha256_hex
from apps.cycles.fiscal_scope import awaiting_cycle_review, review_head
from apps.cycles.services.cycle import tenant_id
from apps.obligations.models import FiscalDocument


def fiscal_documents(context: SourceContext) -> SourceQuery:
    """Project a private source in the existing scoped briefing UNION."""
    assigned = ClientAssignment.objects.filter(
        client_id=OuterRef("client_id"), role=AssignmentRole.PRIMARY
    ).order_by("pk")
    rows = awaiting_cycle_review(
        FiscalDocument.objects.filter(
            client_id__in=context.clients.values("pk"), source="captura"
        )
    )
    if not tenant_has(tenant_id(), "document_capture"):
        rows = rows.none()
    pending_head = review_head(
        cycle_id=OuterRef("pending_cycle_id"),
        revision_id=OuterRef("current_revision_id"),
    ).values("pk")[:1]
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
            brief_version=sha256_hex(
                JSONArray(
                    Value("fiscal-pending:v1"),
                    "pending_cycle_id",
                    "current_revision_id",
                    "review_material",
                    Subquery(pending_head),
                )
            ),
            brief_observed=F("current_revision__observed_at"),
            brief_target=F("pending_cycle_id"),
            brief_title=Value("Documento capturado aguardando revisão"),
            brief_action=Value("Conferir a versão e os arquivos retidos"),
            brief_service=Value(""),
            brief_state=Value("aguardando_revisao"),
            brief_visible=Value(value=False),
        )
    )
