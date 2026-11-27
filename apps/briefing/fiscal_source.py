"""Captured fiscal heads awaiting their own revision's accountant decision."""

from __future__ import annotations

from django.db.models import (
    Case,
    DateField,
    F,
    OuterRef,
    QuerySet,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Cast, Coalesce, JSONArray, TruncMonth

from apps.briefing.source_rows import SourceContext, SourceQuery, project
from apps.clients.models import AssignmentRole, ClientAssignment
from apps.core.flags import tenant_has
from apps.cycles.fiscal_material import sha256_hex
from apps.cycles.fiscal_scope import projected_pending_cycles, review_head
from apps.cycles.services.cycle import tenant_id
from apps.obligations.models import FiscalDocument


def projected_fiscal_documents(context: SourceContext) -> SourceQuery:
    """Project the cycle, revision, material and terminal head as one pending fact."""
    rows = projected_pending_cycles(
        FiscalDocument.objects.filter(
            client_id__in=context.clients.values("pk"), source="captura"
        )
    )
    pending_head = review_head(
        cycle_id=OuterRef("pending_cycle_id"),
        revision_id=OuterRef("current_revision_id"),
    ).values("pk")[:1]
    return _project_fiscal(
        rows.annotate(
            brief_version=sha256_hex(
                JSONArray(
                    Value("fiscal-pending:v1"),
                    "pending_cycle_id",
                    "current_revision_id",
                    "review_material",
                    Subquery(pending_head),
                )
            ),
            brief_target=F("pending_cycle_id"),
        )
    )


def _project_fiscal(rows: QuerySet[FiscalDocument]) -> SourceQuery:
    assigned = ClientAssignment.objects.filter(
        client_id=OuterRef("client_id"), role=AssignmentRole.PRIMARY
    ).order_by("pk")
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
            brief_observed=F("current_revision__observed_at"),
            brief_title=Value("Documento capturado aguardando revisão"),
            brief_action=Value("Conferir a versão e os arquivos retidos"),
            brief_service=Value(""),
            brief_state=Value("aguardando_revisao"),
            brief_visible=Value(value=False),
        )
    )
