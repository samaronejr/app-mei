"""Statement-scoped fiscal reads; projections remain lazy until the caller selects."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from django.contrib.postgres.expressions import ArraySubquery
from django.db.models import Exists, OuterRef, Q
from django.db.models.functions import JSONObject
from django.shortcuts import get_object_or_404

from apps.cycles.fiscal_material import Material, snapshot_rows
from apps.cycles.fiscal_records import FiscalDocumentReview
from apps.cycles.fiscal_scope import reviewable_documents
from apps.cycles.records import ServiceCycle
from apps.obligations.models import FiscalDocument, FiscalRevision, RevisionArtifact

if TYPE_CHECKING:
    from uuid import UUID

    from django.db.models import QuerySet
    from django_stubs_ext import WithAnnotations


class CycleRow(TypedDict):
    """Selectable or historical cycle, including its version in the statement."""

    pk: str
    state: str
    period_start: str
    version: int
    eligible: bool


class ReviewRow(TypedDict):
    """Immutable decision with its chain edge and the version actually reviewed."""

    pk: str
    cycle_id: str
    revision_id: str
    supersedes_id: str | None
    input_digest: str
    decision: str
    reviewed_at: str
    reviewer: str
    note: str
    provider_version: str
    source_namespace: str


class RevisionRow(TypedDict):
    """Source revision with the retained or purged artifact links seen with it."""

    pk: str
    provider_version: str
    fiscal_state: str
    source_namespace: str
    observed_at: str
    source_effective_at: str | None
    replacement_key: str
    artifacts: list[dict[str, str | None]]


class PreviewFacts(Material):
    """All facts needed by a workspace without subsequent ORM lookups."""

    review_cycles: list[CycleRow]
    review_history: list[ReviewRow]
    revision_rows: list[RevisionRow]


def load_snapshot(pk: UUID) -> WithAnnotations[FiscalDocument, PreviewFacts]:
    """Select one document, not a product of its cycles, history and artifacts."""
    eligible = reviewable_documents(
        client_id=OuterRef("client_id"),
        start=OuterRef("period_start"),
        end=OuterRef("period_end"),
    ).filter(pk=OuterRef(OuterRef("pk")))
    history = FiscalDocumentReview.objects.filter(
        document_id=OuterRef(OuterRef("pk")), cycle_id=OuterRef("pk")
    )
    cycles = (
        ServiceCycle.objects.filter(client_id=OuterRef("client_id"))
        .annotate(eligible=Exists(eligible))
        .filter(Q(eligible=True) | Exists(history))
        .order_by("period_start", "pk")
    )
    reviews = FiscalDocumentReview.objects.filter(document_id=OuterRef("pk")).order_by(
        "reviewed_at", "pk"
    )
    revisions = FiscalRevision.objects.filter(document_id=OuterRef("pk")).order_by(
        "-observed_at", "-pk"
    )
    artifacts = RevisionArtifact.objects.filter(revision_id=OuterRef("pk")).order_by(
        "role", "pk"
    )
    rows: QuerySet[WithAnnotations[FiscalDocument, PreviewFacts]] = snapshot_rows(
        FiscalDocument.objects.select_related("client")
    ).annotate(
        review_cycles=ArraySubquery(
            cycles.values(
                row=JSONObject(
                    pk="pk",
                    state="state",
                    period_start="period_start",
                    version="version",
                    eligible="eligible",
                )
            )
        ),
        review_history=ArraySubquery(
            reviews.values(
                row=JSONObject(
                    pk="pk",
                    cycle_id="cycle_id",
                    revision_id="revision_id",
                    supersedes_id="supersedes_id",
                    input_digest="input_digest",
                    decision="decision",
                    reviewed_at="reviewed_at",
                    reviewer="reviewer__email",
                    note="note",
                    provider_version="revision__provider_version",
                    source_namespace="revision__source_namespace",
                )
            )
        ),
        revision_rows=ArraySubquery(
            revisions.values(
                row=JSONObject(
                    pk="pk",
                    provider_version="provider_version",
                    fiscal_state="fiscal_state",
                    source_namespace="source_namespace",
                    observed_at="observed_at",
                    source_effective_at="source_effective_at",
                    replacement_key="replacement_key",
                    artifacts=ArraySubquery(
                        artifacts.values(
                            row=JSONObject(
                                role="role",
                                document_id="document_id",
                                purged_at="document__purged_at",
                            )
                        )
                    ),
                )
            )
        ),
    )
    return get_object_or_404(rows, pk=pk)
