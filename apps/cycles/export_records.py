"""Private evidence projections and recoverable archive attempts."""

from typing import ClassVar
from uuid import uuid4

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.clients.models import OnboardingStatus
from apps.cycles.records import ClientRecord


class EvidenceIndex(ClientRecord):
    """An immutable minimized projection, never document text or credentials."""

    cycle = models.ForeignKey(
        "cycles.ServiceCycle", on_delete=models.PROTECT, related_name="evidence_indexes"
    )
    amendment = models.PositiveIntegerField()
    cycle_revision = models.PositiveIntegerField()
    source_digest = models.CharField(max_length=64)
    digest = models.CharField(max_length=64)
    data = models.JSONField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)


class ExportState(models.TextChoices):
    """Archive readiness is unrelated to fiscal or provider verification."""

    PENDING = OnboardingStatus.PENDING.value, "Na fila"
    BUILDING = "building", "Preparando arquivo"
    READY = "ready", "Disponível para download"
    FAILED = "failed", "Não foi possível gerar"
    EXPIRED = "expired", "Prazo de download encerrado"


class EvidenceExport(ClientRecord):
    """One requested immutable projection with fenced, disposable byte attempts."""

    cycle = models.ForeignKey(
        "cycles.ServiceCycle", on_delete=models.PROTECT, related_name="evidence_exports"
    )
    index = models.ForeignKey(EvidenceIndex, on_delete=models.PROTECT)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    state = models.CharField(
        max_length=16, choices=ExportState.choices, default=ExportState.PENDING
    )
    version = models.PositiveIntegerField(default=1)
    fence = models.PositiveBigIntegerField(default=0)
    lease_until = models.DateTimeField(null=True)
    storage_key = models.CharField(max_length=255, blank=True)
    byte_size = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True)
    expires_at = models.DateTimeField(null=True)

    class Meta(ClientRecord.Meta):
        """Restrict the persisted state to the recoverable export lifecycle."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.CheckConstraint(
                condition=models.Q(state__in=ExportState.values),
                name="evidenceexport_known_state",
            ),
        ]


class ExportAttempt(ClientRecord):
    """An exact owned key survives a worker crash and failed cleanup."""

    export = models.ForeignKey(EvidenceExport, on_delete=models.PROTECT)
    fence = models.PositiveBigIntegerField()
    owner = models.UUIDField(default=uuid4)
    storage_key = models.CharField(max_length=255)
    cleanup_pending = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(ClientRecord.Meta):
        """Give each client-scoped export fence exactly one owned attempt."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "client", "export", "fence"],
                name="exportattempt_fence_uniq",
            ),
        ]
