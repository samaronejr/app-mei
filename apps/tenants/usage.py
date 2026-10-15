"""Daily immutable metering and owner-only estimates from configured unit costs."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid5

from django.conf import settings
from django.db.models import Count, Max, Sum
from django.http import Http404

from apps.accounts.models import User
from apps.authz.firm_account import require_account_owner
from apps.core.tenancy import current_tenant_id, tenant_context
from apps.delivery.models import DeliveryEffect, MessageState
from apps.obligations.models import Document
from apps.tenants.entitlements import active_client_count
from apps.tenants.models import (
    FirmMessageState,
    FirmOutboundMessage,
    UsageKind,
    UsageMetric,
    UsageRecord,
)


def record_tenant_usage(tenant_id: UUID, *, on: date) -> int:
    """Capture gauges now and the complete prior day's sends; T24 owns AI events."""
    with tenant_context(tenant_id):
        storage = Document.objects.filter(purged_at=None).aggregate(
            size=Sum("byte_size", default=0)
        )["size"]
        email_day = on - timedelta(days=1)
        emails = DeliveryEffect.objects.filter(
            outcome=MessageState.ACCEPTED, occurred_at__date=email_day
        ).count()
        emails += FirmOutboundMessage.objects.filter(
            state=FirmMessageState.ACCEPTED, finished_at__date=email_day
        ).count()
        measurements = (
            (
                UsageMetric.ACTIVE_CLIENTS,
                on,
                active_client_count(),
                UsageKind.CONFIRMED,
            ),
            (UsageMetric.STORAGE_BYTES, on, int(storage), UsageKind.ESTIMATED),
            (UsageMetric.EMAILS_SENT, email_day, emails, UsageKind.CONFIRMED),
        )
        created = 0
        for metric, period, quantity, kind in measurements:
            _record, added = UsageRecord.objects.get_or_create(
                metric=metric,
                source_event=uuid5(tenant_id, f"daily:{metric}:{period}"),
                defaults={
                    "tenant_id": tenant_id,
                    "period": period,
                    "quantity": quantity,
                    "kind": kind,
                },
            )
            created += int(added)
        return created


@dataclass(frozen=True, slots=True)
class UsageLine:
    """A gauge's latest reading or a flow's monthly sum, with explicit missingness."""

    metric: str
    label: str
    quantity: int | None
    unit_price: Decimal | None
    covered_through: date | None = None
    measured_days: int | None = None
    partial: bool = False

    @property
    def cost(self) -> Decimal | None:
        """Never turn an unconfigured price or missing measurement into zero cost."""
        if self.quantity is None or self.unit_price is None:
            return None
        return Decimal(self.quantity) * self.unit_price


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """Configured unit costs produce an estimate, never an invoice."""

    period: date
    lines: tuple[UsageLine, ...]

    @property
    def complete(self) -> bool:
        """Report whether every component has a measured quantity and known price."""
        return all(line.cost is not None and not line.partial for line in self.lines)

    @property
    def total(self) -> Decimal | None:
        """Only a complete estimate can be presented as a monthly total."""
        if not self.complete:
            return None
        return sum(
            (line.cost for line in self.lines if line.cost is not None), Decimal(0)
        )


def cost_estimate(actor: User, tenant_id: UUID, *, on: date) -> CostEstimate:
    """Authorize the owner before resolving any firm-wide usage quantities."""
    if current_tenant_id.get() != tenant_id:
        raise Http404
    require_account_owner(actor, tenant_id)
    start = on.replace(day=1)
    corrected = UsageRecord.objects.filter(correction_of__isnull=False).values(
        "correction_of_id"
    )
    current = UsageRecord.objects.exclude(pk__in=corrected).filter(
        period__gte=start, period__lte=on
    )
    lines = []
    for metric in UsageMetric:
        readings = current.filter(metric=metric)
        covered_through = None
        measured_days = None
        partial = False
        if metric == UsageMetric.EMAILS_SENT:
            coverage = readings.aggregate(
                through=Max("period"), days=Count("period", distinct=True)
            )
            covered_through = coverage["through"]
            measured_days = coverage["days"]
            partial = measured_days < on.day
        quantity = (
            readings.order_by("-period", "-created_at")
            .values_list("quantity", flat=True)
            .first()
            if metric in {UsageMetric.ACTIVE_CLIENTS, UsageMetric.STORAGE_BYTES}
            else readings.aggregate(
                total=Sum(
                    "quantity",
                    default=None if metric == UsageMetric.EMAILS_SENT else 0,
                )
            )["total"]
        )
        price = settings.USAGE_UNIT_PRICES_BRL.get(metric)
        lines.append(
            UsageLine(
                metric,
                str(metric.label),
                int(quantity) if quantity is not None else None,
                Decimal(str(price)) if price is not None else None,
                covered_through,
                measured_days,
                partial,
            )
        )
    return CostEstimate(start, tuple(lines))
