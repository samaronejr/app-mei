"""Serialize budget reservations from AgentRun, without bypassing tenant RLS."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from django.conf import settings
from django.db import connection
from django.db.models import Sum
from django.utils import timezone

from apps.automation.models import AgentRun
from apps.automation.types import Budget, RunState
from apps.clients.models import ClientCompany
from apps.core.flags import tenant_has
from apps.core.tasks import PlatformTask
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake, EvidenceLink, IntakeState, ScanVerdict

BUDGET_LOCK: Final = 240091
DOC_CAP: Final = Decimal("0.05")
MONTH_CAP: Final = Decimal("50.00")
CALLS_PER_DOC: Final = 2
STATES: Final = (IntakeState.CLASSIFIED, IntakeState.LINKED)


@dataclass(frozen=True, slots=True)
class Ticket:
    """Only one source and its trusted scope leave the admission transaction."""

    run_id: UUID
    tenant_id: UUID
    intake: DocumentIntake
    client_cnpj: str
    expected_documents: tuple[str, ...]
    budget: Budget


def enabled(tenant_id: UUID) -> bool:
    """All gates must agree; real-data approval cannot be inferred from a key."""
    return (
        not settings.AI_GLOBAL_DISABLED
        and bool(settings.AI_PROVIDER)
        and tenant_has(tenant_id, "ai_shadow")
        and (
            settings.AI_PROVIDER == "fake"
            or (settings.AI_PROVIDER == "openai" and settings.AI_REAL_DATA_APPROVED)
        )
    )


def eligible(tenant_id: UUID, intake_id: UUID) -> bool:
    """Check the actual clean linked source before putting anything on the broker."""
    if not enabled(tenant_id):
        return False
    with tenant_context(tenant_id):
        return DocumentIntake.objects.filter(
            pk=intake_id,
            state__in=STATES,
            verdict=ScanVerdict.CLEAN,
            document__evidence_links__isnull=False,
        ).exists()


def _reserved_since(tenant_id: UUID, since: datetime) -> Decimal:
    with tenant_context(tenant_id):
        total = AgentRun.objects.filter(created_at__gte=since).aggregate(
            total=Sum("reserved_brl")
        )["total"]
        return Decimal(total or 0)


def claim(tenant_id: UUID, intake_id: UUID, *, now: datetime) -> Ticket | None:
    """Atomically reserve the upper bound before any provider I/O.

    The advisory transaction lock serializes ALL firms. Each aggregate still runs
    inside exactly one tenant_context, including inactive firms' earlier spend.
    Reservations are never refunded on uncertainty and survive content expiry.
    """
    if not enabled(tenant_id):
        return None
    with tenant_context(tenant_id):
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [BUDGET_LOCK])
        intake = (
            DocumentIntake.objects.select_for_update(of=("self",))
            .select_related("document")
            .filter(pk=intake_id, state__in=STATES, verdict=ScanVerdict.CLEAN)
            .first()
        )
        if (
            intake is None
            or not enabled(tenant_id)
            or not EvidenceLink.objects.filter(document_id=intake.document_id).exists()
        ):
            return None
        links = EvidenceLink.objects.filter(document_id=intake.document_id)
        client = ClientCompany.objects.get(pk=intake.client_id)
        previous = AgentRun.objects.filter(
            client_id=intake.client_id, input_hash=intake.document.sha256
        )
        budget = Budget()
        if (
            previous.count() >= CALLS_PER_DOC
            or previous.filter(
                state__in=(RunState.PENDING, RunState.COMPLETED)
            ).exists()
            or sum(previous.values_list("reserved_brl", flat=True), Decimal(0))
            + budget.max_brl
            > DOC_CAP
        ):
            return None
        local = timezone.localtime(now)
        day = datetime.combine(local.date(), time.min, tzinfo=local.tzinfo)
        month = day.replace(day=1)
        if AgentRun.objects.filter(
            created_at__gte=day
        ).count() >= settings.AI_SHADOW_TENANT_DAILY_CALLS or _reserved_since(
            tenant_id, day
        ) + budget.max_brl > Decimal(settings.AI_SHADOW_TENANT_DAILY_BRL):
            return None
        global_spend = sum(
            (
                _reserved_since(tid, month)
                for tid in PlatformTask.tenant_ids(include_inactive=True)
            ),
            Decimal(0),
        )
        if global_spend + budget.max_brl > MONTH_CAP:
            return None
        run = AgentRun.objects.create(
            tenant_id=tenant_id,
            client_id=intake.client_id,
            document_id=intake.document_id,
            input_hash=intake.document.sha256,
            model_id="fake-v1" if settings.AI_PROVIDER == "fake" else settings.AI_MODEL,
            reserved_brl=budget.max_brl,
            created_at=now,
            expires_at=now + timedelta(days=365),
        )
        expected = tuple(
            f"{pk}: {title}"
            for pk, title in links.values_list("request_id", "request__title")
        )
        return Ticket(run.pk, tenant_id, intake, client.cnpj, expected, budget)
