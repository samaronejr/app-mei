"""Portfolio and period projections for fiscal identities and exact reviews."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from django.contrib.postgres.expressions import ArraySubquery
from django.contrib.postgres.fields import ArrayField
from django.db.models import (
    Case,
    F,
    JSONField,
    OuterRef,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import JSONObject

from apps.authz.portfolio import visible_clients
from apps.cycles.fiscal_material import snapshot_rows
from apps.cycles.fiscal_scope import (
    projected_pending_cycles,
    review_head,
    reviewable_documents,
)
from apps.cycles.services.cycle import tenant_id
from apps.obligations.models import FiscalDocument

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django_stubs_ext import WithAnnotations

    from apps.accounts.models import User
    from apps.cycles.fiscal_scope import PendingReview


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


def projected_pending_documents(
    actor: User,
) -> QuerySet[WithAnnotations[FiscalDocument, PendingReview]]:
    """List the actor's documents that still need a cycle decision."""
    return (
        projected_pending_cycles(
            FiscalDocument.objects.filter(
                client_id__in=visible_clients(actor, tenant_id=tenant_id()).values("pk")
            )
        )
        .select_related("client", "current_revision")
        .order_by("created_at", "pk")
    )


def projected_period_document_rows(*, enabled: bool) -> ArraySubquery | Value:
    """Join the exact-cycle decision only if its terminal head is current."""
    if not enabled:
        return Value([], output_field=ArrayField(JSONField()))
    reviews = review_head(
        cycle_id=OuterRef(OuterRef("pk")),
        revision_id=OuterRef("current_revision_id"),
    )
    rows = (
        snapshot_rows(
            reviewable_documents(
                client_id=OuterRef("client_id"),
                start=OuterRef("period_start"),
                end=OuterRef("period_end"),
            )
        )
        .alias(head_material=Subquery(reviews.values("input_digest")[:1]))
        .order_by("issue_date", "pk")
    )
    return _period_rows(
        rows,
        Case(
            When(
                head_material=F("review_material"),
                then=Subquery(reviews.values("decision")[:1]),
            )
        ),
    )


def _period_rows(
    rows: QuerySet[FiscalDocument], decision: Subquery | Case
) -> ArraySubquery:
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
                review_decision=decision,
            )
        )
    )
