"""A review chain and its current document/cycle scope."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from django.db.models import Exists, F, OuterRef, Q, Subquery, Value
from django.db.models.lookups import Exact, GreaterThanOrEqual, IsNull, LessThanOrEqual
from django.http import Http404

from apps.cycles.fiscal_material import material_digest, snapshot_rows, working_date
from apps.cycles.fiscal_records import FiscalDocumentReview
from apps.cycles.records import ServiceCycle
from apps.cycles.services.cycle import OPEN_STATES
from apps.obligations.models import FiscalDocument

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date
    from uuid import UUID

    from django.db.models import QuerySet
    from django_stubs_ext import WithAnnotations


def review_head(
    *, cycle_id: UUID | OuterRef, revision_id: UUID | OuterRef
) -> QuerySet[FiscalDocumentReview]:
    """Select the terminal row for the exact cycle/revision, regardless of digest."""
    successors = FiscalDocumentReview.objects.filter(supersedes_id=OuterRef("pk"))
    return FiscalDocumentReview.objects.filter(
        cycle_id=cycle_id, revision_id=revision_id
    ).filter(~Exists(successors))


def cycle_eligibility(
    document: type[F], *, client_id: UUID | F, start: date | F, end: date | F
) -> Q:
    """Bind a current fiscal revision to its client and inclusive working period."""
    day = document("review_working_date")
    return Q(
        Exact(document("client_id"), client_id),
        IsNull(document("current_revision_id"), rhs=False),
        ~Q(Exact(document("role_verification"), Value("divergent"))),
        GreaterThanOrEqual(day, start),
        LessThanOrEqual(day, end),
    )


def reviewable_documents(
    *, client_id: UUID | F, start: date | F, end: date | F
) -> QuerySet[FiscalDocument]:
    """Resolve the document side of the same match used by the pending queue."""
    return FiscalDocument.objects.alias(review_working_date=working_date()).filter(
        cycle_eligibility(F, client_id=client_id, start=start, end=end)
    )


class PendingReview(TypedDict):
    """The oldest applicable open cycle still needing a review."""

    pending_cycle_id: UUID | None


def awaiting_cycle_review(
    rows: QuerySet[FiscalDocument],
) -> QuerySet[WithAnnotations[FiscalDocument, PendingReview]]:
    """Keep documents with a pending open cycle or no matching cycle yet."""
    return _pending_cycles(
        rows.filter(current_revision__isnull=False).alias(
            review_working_date=working_date(), review_material=material_digest()
        )
    )


def projected_pending_cycles(
    rows: QuerySet[FiscalDocument],
) -> QuerySet[WithAnnotations[FiscalDocument, PendingReview]]:
    """Expose the same material used to select the pending cycle in its result."""
    return _pending_cycles(
        snapshot_rows(rows.filter(current_revision__isnull=False)).alias(
            review_working_date=working_date()
        )
    )


def _pending_cycles(
    rows: QuerySet[FiscalDocument],
) -> QuerySet[WithAnnotations[FiscalDocument, PendingReview]]:
    eligible = ServiceCycle.objects.filter(
        cycle_eligibility(
            OuterRef,
            client_id=F("client_id"),
            start=F("period_start"),
            end=F("period_end"),
        )
    )
    heads = review_head(
        cycle_id=OuterRef("pk"),
        revision_id=OuterRef(OuterRef("current_revision_id")),
    )
    pending_cycles = (
        eligible.filter(state__in=OPEN_STATES)
        .filter(
            ~Exists(heads.filter(input_digest=OuterRef(OuterRef("review_material"))))
        )
        .order_by("period_start", "pk")
    )
    return rows.annotate(
        pending_cycle_id=Subquery(pending_cycles.values("pk")[:1])
    ).filter(Q(pending_cycle_id__isnull=False) | ~Exists(eligible))


class CycleEligibility(TypedDict):
    """Whether a cycle covers the document now, not only its history."""

    review_eligible: bool


def review_cycles(
    fiscal: FiscalDocument, requested: UUID | None
) -> tuple[Sequence[ServiceCycle], ServiceCycle | None, bool]:
    """Resolve an explicit cycle (or choose a pending one for an unbound read)."""
    match_period = reviewable_documents(
        client_id=OuterRef("client_id"),
        start=OuterRef("period_start"),
        end=OuterRef("period_end"),
    ).filter(pk=fiscal.pk)
    history = FiscalDocumentReview.objects.filter(
        document=fiscal, cycle_id=OuterRef("pk")
    )
    rows: QuerySet[WithAnnotations[ServiceCycle, CycleEligibility]] = (
        ServiceCycle.objects.filter(client_id=fiscal.client_id)
        .annotate(review_eligible=Exists(match_period))
        .filter(Q(review_eligible=True) | Exists(history))
        .order_by("period_start", "pk")
    )
    cycles = list(rows)
    if requested is not None:
        selected = next((cycle for cycle in cycles if cycle.pk == requested), None)
        if selected is None:
            raise Http404
    else:
        eligible = [cycle for cycle in cycles if cycle.review_eligible]
        pending_id = (
            awaiting_cycle_review(FiscalDocument.objects.filter(pk=fiscal.pk))
            .values_list("pending_cycle_id", flat=True)
            .first()
        )
        selected = next(
            (cycle for cycle in eligible if cycle.pk == pending_id),
            next(iter(eligible or cycles), None),
        )
    return cycles, selected, selected is not None and selected.review_eligible
