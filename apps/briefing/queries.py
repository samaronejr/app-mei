"""The daily queue: one live portfolio decision before any provider or count."""

from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from logging import getLogger
from uuid import UUID

from django.contrib.postgres.expressions import ArraySubquery
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.db.models import OuterRef, QuerySet
from django.db.models.functions import JSONObject
from django.http import Http404
from django.urls import reverse
from django.utils import timezone
from django.utils.timezone import now as clock

from apps.accounts.models import User
from apps.authz.portfolio import visible_clients
from apps.briefing.cursors import cursor_for, cursor_key, selection_key
from apps.briefing.providers import (
    COVERAGE_PROVIDER,
    PROVIDER_LABELS,
    isolated_source_items,
    source_items,
)
from apps.briefing.source_rows import SourceContext
from apps.briefing.types import (
    BriefingChange,
    BriefingCount,
    BriefingFilters,
    BriefingItem,
    BriefingPage,
    BriefingScope,
)
from apps.clients.models import ClientCompany, TaxProfile, TaxRegime
from apps.clients.services import claim_at
from apps.core.flags import tenant_has
from apps.cycles.models import BriefingFeedback, CycleEvent
from apps.cycles.services.cycle import tenant_id
from apps.cycles.workspace import EVENT_LABELS

__all__ = ["briefing", "clock"]

PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100
logger = getLogger(__name__)
CHANGE_LABELS = {
    **EVENT_LABELS,
    "proposal.created": "Proposta pronta para revisão",
    "evidence.submitted": "Documento recebido",
    "document.reviewed": "Documento revisado",
    "exception.opened": "Pendência aberta",
}


def _counts(
    items: Sequence[BriefingItem], failed: tuple[str, ...]
) -> tuple[BriefingCount, ...]:
    counted = Counter(item.kind for item in items)
    return tuple(
        BriefingCount(kind, label, counted[kind])
        for kind, label in PROVIDER_LABELS.items()
        if kind not in failed
    )


def _matches(
    item: BriefingItem,
    filters: BriefingFilters,
    profiles: Sequence[TaxProfile],
) -> bool:
    matches = (
        filters.period is None or item.period == filters.period,
        filters.responsible is None or item.owner_id == filters.responsible,
        not filters.service or item.service_kind == filters.service,
        not filters.work_state or item.work_state == filters.work_state,
        not filters.risk_reason or filters.risk_reason in dict(item.factors),
        not filters.freshness or item.freshness == filters.freshness,
    )
    if not all(matches):
        return False
    if filters.regime:
        if item.period is None:
            return filters.regime == TaxRegime.UNKNOWN
        profile = claim_at(profiles, item.period)
        regime = profile.regime if profile is not None else TaxRegime.UNKNOWN
        if regime != filters.regime:
            return False
    return True


def _read_items(
    actor: User,
    context: SourceContext,
    client_rows: Sequence[ClientCompany],
    period: date,
) -> tuple[list[BriefingItem], tuple[str, ...]]:
    if not client_rows:
        return [], ()
    try:
        with transaction.atomic():
            items = source_items(context)
            items.extend(COVERAGE_PROVIDER(actor, client_rows, period, now=context.now))
    except DatabaseError:
        logger.exception("Briefing batch unavailable; isolating source failures")
    else:
        return items, ()
    items, failed = isolated_source_items(context)
    try:
        with transaction.atomic():
            coverage = COVERAGE_PROVIDER(actor, client_rows, period, now=context.now)
    except DatabaseError:
        logger.exception("Briefing coverage unavailable")
        return items, (*failed, "coverage")
    items.extend(coverage)
    return items, failed


def _changes(
    clients: QuerySet[ClientCompany],
    actor: User,
    scope: BriefingScope,
    filters: BriefingFilters,
    moment: datetime,
) -> tuple[BriefingChange, ...]:
    yesterday = timezone.localtime(moment).date() - timedelta(days=1)
    since = timezone.make_aware(datetime.combine(yesterday, time.min))
    events = CycleEvent.objects.filter(
        client_id__in=clients.values("pk"),
        occurred_at__gte=since,
        occurred_at__lte=moment,
    )
    if scope == "me":
        events = events.filter(cycle__owner=actor)
    if filters.responsible is not None:
        events = events.filter(cycle__owner_id=filters.responsible)
    if filters.period is not None:
        events = events.filter(cycle__period_start=filters.period)
    if filters.service:
        events = events.filter(cycle__engagement__service_kind=filters.service)
    recent = events.select_related("client").order_by("-occurred_at", "-pk")[:20]
    return tuple(
        BriefingChange(
            client_name=event.client.legal_name,
            label=CHANGE_LABELS.get(event.type, "Ciclo atualizado"),
            occurred_at=event.occurred_at,
            url=reverse("cycle-detail", args=[event.cycle_id]),
        )
        for event in recent
    )


def briefing(
    actor: User,
    *,
    scope: BriefingScope,
    filters: BriefingFilters,
    cursor: str | None = None,
    limit: int = PAGE_LIMIT,
) -> BriefingPage:
    """Read current facts and re-authorize even when filters came from a saved view."""
    if not tenant_has(tenant_id(), "briefing_r1"):
        raise Http404
    if scope not in {"me", "team"} or not 1 <= limit <= MAX_PAGE_LIMIT:
        message = "Escopo ou tamanho de página inválido."
        raise ValidationError(message)
    moment = clock()
    selection = selection_key(actor, scope, filters)
    boundary = cursor_key(cursor, selection) if cursor else None
    clients = visible_clients(actor, tenant_id=tenant_id())
    if filters.client_id is not None:
        clients = clients.filter(pk=filters.client_id)
    if filters.uf:
        clients = clients.filter(state=filters.uf)
    if filters.municipality:
        clients = clients.filter(municipality_ibge_code=filters.municipality)
    personal_feedback = BriefingFeedback.objects.filter(
        user=actor, client_id=OuterRef("pk")
    ).values(
        data=JSONObject(
            kind="kind", subject_ref="subject_ref", fact_version="fact_version"
        )
    )
    client_rows = list(
        clients.annotate(briefing_feedback_keys=ArraySubquery(personal_feedback))
    )
    period = filters.period or timezone.localtime(moment).date().replace(day=1)
    profiles: dict[UUID, list[TaxProfile]] = {}
    if filters.regime:
        for profile in TaxProfile.objects.filter(
            client_id__in=[client.pk for client in client_rows]
        ).order_by("client_id", "-valid_from"):
            profiles.setdefault(profile.client_id, []).append(profile)
    items, failed = _read_items(
        actor, SourceContext(clients, moment), client_rows, period
    )
    owners = tuple(
        sorted(
            {
                (item.owner_id, item.owner_name)
                for item in items
                if item.owner_id is not None
            },
            key=lambda owner: (owner[1].casefold(), str(owner[0])),
        )
    )
    dismissals = {
        (row["kind"], UUID(row["subject_ref"]), row["fact_version"])
        for client in client_rows
        for row in client.briefing_feedback_keys
    }
    visible = [
        item
        for item in items
        if (scope == "team" or item.owner_id == actor.pk)
        and _matches(item, filters, profiles.get(item.client_id, ()))
        and (item.kind, item.subject_ref, item.fact_version) not in dismissals
    ]
    counts = _counts(visible, failed)
    visible.sort(key=lambda item: item.order_key)
    eligible = [
        item for item in visible if boundary is None or item.order_key > boundary
    ]
    page_items = eligible[:limit]
    next_cursor = (
        cursor_for(page_items[-1], selection) if len(eligible) > limit else None
    )
    return BriefingPage(
        items=tuple(page_items),
        counts=counts,
        total=len(visible),
        next_cursor=next_cursor,
        computed_at=moment,
        changes=_changes(clients, actor, scope, filters, moment),
        owners=owners,
        failed_providers=failed,
    )
