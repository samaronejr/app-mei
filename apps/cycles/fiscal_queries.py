"""Portfolio and period projections for fiscal identities and exact reviews."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from django.contrib.postgres.expressions import ArraySubquery
from django.contrib.postgres.fields import ArrayField
from django.db.models import Exists, JSONField, OuterRef, Q, Subquery, Value
from django.db.models.functions import JSONObject

from apps.authz.portfolio import visible_clients
from apps.cycles.fiscal_records import FiscalDocumentReview
from apps.cycles.services.cycle import tenant_id
from apps.obligations.models import FiscalDocument

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from apps.accounts.models import User


class PeriodDocumentRow(TypedDict):
    """Scalar display facts selected with the authorized cycle row."""

    pk: str
    document_key: str
    family: str
    counterpart_name: str
    fiscal_state: str
    source: str
    provider: str
    environment: str
    summary_only: bool
    provider_version: str
    observed_at: str
    review_decision: str | None


def pending_documents(actor: User) -> QuerySet[FiscalDocument]:
    """Return a successor head to the queue without changing the old decision."""
    reviewed = FiscalDocumentReview.objects.filter(
        revision_id=OuterRef("current_revision_id")
    )
    return (
        FiscalDocument.objects.filter(
            client_id__in=visible_clients(actor, tenant_id=tenant_id()).values("pk"),
            current_revision__isnull=False,
        )
        .filter(~Exists(reviewed))
        .select_related("client", "current_revision")
        .order_by("created_at", "pk")
    )


def period_document_rows(*, enabled: bool) -> ArraySubquery | Value:
    """Join monthly fiscal facts into the cycle read without a separate query."""
    if not enabled:
        return Value([], output_field=ArrayField(JSONField()))
    reviews = FiscalDocumentReview.objects.filter(
        cycle_id=OuterRef(OuterRef("pk")),
        revision_id=OuterRef("current_revision_id"),
    )
    rows = (
        FiscalDocument.objects.filter(
            Q(
                family="nfe",
                issue_date__range=(OuterRef("period_start"), OuterRef("period_end")),
            )
            | Q(
                family__in=("nfse_nacional", "nfse_municipal"),
                competence__range=(OuterRef("period_start"), OuterRef("period_end")),
            ),
            client_id=OuterRef("client_id"),
            current_revision__isnull=False,
        )
        .exclude(role_verification="divergent")
        .order_by("issue_date", "pk")
    )
    return ArraySubquery(
        rows.values(
            data=JSONObject(
                pk="pk",
                document_key="document_key",
                family="family",
                counterpart_name="counterpart_name",
                fiscal_state="fiscal_state",
                source="source",
                provider="provider",
                environment="environment",
                summary_only="summary_only",
                provider_version="current_revision__provider_version",
                observed_at="current_revision__observed_at",
                review_decision=Subquery(reviews.values("decision")[:1]),
            )
        )
    )
