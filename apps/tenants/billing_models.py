"""The SaaS account and its private usage ledger, never the firm's client fees."""

from typing import ClassVar

from django.db import models
from django.utils import timezone

from apps.core.models import TenantScopedModel


class BillingProvider(models.TextChoices):
    """R1 records the operator's manual billing process."""

    MANUAL = "manual", "Manual"


class BillingStatus(models.TextChoices):
    """An operator assertion about the subscription, not a payment receipt."""

    ACTIVE = "active", "Ativa (registro manual)"
    PAST_DUE = "past_due", "Pendência de cobrança"
    SUSPENDED = "suspended", "Suspensa"


class BillingAccount(TenantScopedModel):
    """The operator-maintained subscription contains no payment credentials."""

    provider = models.CharField(
        max_length=16, choices=BillingProvider.choices, default=BillingProvider.MANUAL
    )
    plan_band = models.CharField(max_length=64, blank=True)
    next_invoice_on = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=BillingStatus.choices, default=BillingStatus.ACTIVE
    )
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantScopedModel.Meta):
        """One subscription per firm, with an explicit manual-only provider."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["tenant"], name="billing_account_tenant"),
            models.CheckConstraint(
                condition=models.Q(provider=BillingProvider.MANUAL),
                name="billing_manual_provider",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=BillingStatus.values, version__gt=0),
                name="billing_known_status_revision",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["tenant", "status"], name="billing_tenant_status_idx"),
        ]


class UsageMetric(models.TextChoices):
    """Count units, not prices; AI records represent attempted provider calls."""

    ACTIVE_CLIENTS = "active_clients", "Clientes ativos"
    STORAGE_BYTES = "storage_bytes", "Armazenamento em bytes"
    EMAILS_SENT = "emails_sent", "E-mails aceitos pelo servidor"
    AI_SHADOW_CALLS = "ai_shadow_calls", "Chamadas de IA em avaliação"


class UsageKind(models.TextChoices):
    """Metering provenance is distinct from fiscal verification."""

    ESTIMATED = "estimated", "Estimado"
    CONFIRMED = "confirmed", "Medido"


class UsageRecord(TenantScopedModel):
    """Immutable measurements; a correction appends a paired successor."""

    metric = models.CharField(max_length=24, choices=UsageMetric.choices)
    period = models.DateField()
    quantity = models.PositiveBigIntegerField()
    source_event = models.UUIDField()
    kind = models.CharField(
        max_length=16, choices=UsageKind.choices, default=UsageKind.CONFIRMED
    )
    correction_of = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="corrections",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantScopedModel.Meta):
        """Idempotent producers and a single correction chain for each measurement."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tenant", "metric", "source_event"], name="usage_source_uniq"
            ),
            models.UniqueConstraint(
                fields=["tenant", "metric", "period", "id"],
                name="usage_correction_scope_uniq",
            ),
            models.UniqueConstraint(
                fields=["tenant", "correction_of"],
                condition=models.Q(correction_of__isnull=False),
                name="usage_single_correction",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    metric__in=UsageMetric.values, kind__in=UsageKind.values
                ),
                name="usage_known_metric_kind",
            ),
            models.CheckConstraint(
                condition=~models.Q(correction_of=models.F("id")),
                name="usage_no_self_correction",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "period", "metric"], name="usage_tenant_period_idx"
            ),
        ]
