"""Registered briefing sources and their explainable, non-persisted items."""

from collections.abc import Callable
from datetime import datetime
from logging import getLogger
from typing import Final
from uuid import UUID

from django.db import DatabaseError, transaction
from django.urls import reverse

from apps.briefing.coverage_source import coverage_items
from apps.briefing.cycle_sources import approvals, exceptions, requests
from apps.briefing.operation_sources import deliveries, documents, obligations
from apps.briefing.rules import PriorityFacts, priority_factors
from apps.briefing.source_rows import SourceContext, SourceQuery, SourceRow
from apps.briefing.types import BriefingItem
from apps.cycles.services.exceptions import reason_for
from apps.obligations.coverage import CoverageState

# Coverage consumes T17's public answer. The other six normally share one
# scoped UNION ALL; a failed read is isolated before naming unavailable sources.
logger = getLogger(__name__)
PROVIDERS: Final[dict[str, Callable[[SourceContext], SourceQuery]]] = {
    "obligation": obligations,
    "request": requests,
    "exception": exceptions,
    "approval": approvals,
    "delivery": deliveries,
    "document": documents,
}
COVERAGE_PROVIDER: Final = coverage_items
PROVIDER_LABELS: Final = {
    "obligation": "Obrigações",
    "request": "Solicitações atrasadas",
    "exception": "Pendências",
    "approval": "Aprovações",
    "delivery": "Envios para revisar",
    "document": "Documentos divergentes",
    "coverage": "Cobertura desatualizada",
}


def _item(row: SourceRow, now: datetime) -> BriefingItem:
    title = row.title
    match row.kind:
        case "obligation":
            facts = PriorityFacts(statutory_due_on=row.due_on)
            url = reverse("client-detail", args=[row.client_id])
            if row.period is not None:
                url += f"?periodo={row.period:%Y-%m}"
        case "request":
            facts = PriorityFacts(request_due_on=row.due_on)
            url = reverse("cycle-detail", args=[row.target_id])
        case "exception":
            facts = PriorityFacts(client_visible_exception=row.client_visible)
            reason = reason_for(row.title)
            title = reason.label if reason is not None else "Pendência do ciclo"
            url = (
                f"{reverse('cycle-detail', args=[row.target_id])}"
                f"?pendencia={row.subject_ref}#pendencias"
            )
        case "approval":
            facts = PriorityFacts(approval_requested_at=row.observed_at)
            url = reverse("approval-card", args=[row.target_id])
        case "delivery":
            facts = PriorityFacts(delivery_problem=True)
            url = (
                f"{reverse('delivery-queue')}?mensagem={row.subject_ref}"
                f"#message-{row.subject_ref}"
            )
        case "document":
            facts = PriorityFacts(divergent_document=True)
            url = (
                reverse("document-review", args=[row.target_id])
                if row.target_id is not None
                else reverse("client-detail", args=[row.client_id])
            )
        case _:
            # Only the registered SQL projections can produce this internal code.
            raise LookupError(row.kind)
    return BriefingItem(
        kind=row.kind,
        subject_ref=row.subject_ref,
        client_id=row.client_id,
        client_name=row.client_name,
        period=row.period,
        owner_id=row.owner_id,
        owner_name=row.owner_name,
        due_on=row.due_on,
        fact_version=row.fact_version,
        factors=priority_factors(facts, now=now),
        source_observed_at=row.observed_at,
        next_action=row.next_action,
        evidence_refs=(str(row.subject_ref),),
        title=title,
        url=url,
        service_kind=row.service_kind,
        work_state=row.work_state,
        freshness=CoverageState.UNKNOWN,
    )


def source_items(context: SourceContext) -> list[BriefingItem]:
    """Execute the registered projections together without an OFFSET or count query."""
    queries = [provider(context) for provider in PROVIDERS.values()]
    combined = queries[0].union(*queries[1:], all=True)
    return [_item(SourceRow(*row), context.now) for row in combined]


def isolated_source_items(
    context: SourceContext,
) -> tuple[list[BriefingItem], tuple[str, ...]]:
    """Identify failed sources after the combined read has been rolled back."""
    items = []
    failed = []
    for kind, provider in PROVIDERS.items():
        try:
            with transaction.atomic():
                available = [
                    _item(SourceRow(*row), context.now) for row in provider(context)
                ]
            items.extend(available)
        except DatabaseError:
            logger.exception("Briefing provider unavailable", extra={"provider": kind})
            failed.append(kind)
    return items, tuple(failed)


def source_item(
    context: SourceContext, kind: str, subject_ref: UUID
) -> BriefingItem | None:
    """Resolve a command's fact through the same scoped provider as the queue."""
    row = PROVIDERS[kind](context).filter(pk=subject_ref).first()
    return _item(SourceRow(*row), context.now) if row is not None else None
