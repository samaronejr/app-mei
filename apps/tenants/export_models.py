"""Tenant-private account archives and their fenced storage attempts."""

from typing import ClassVar
from uuid import uuid4

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TenantScopedModel


class AccountExportState(models.TextChoices):
    """Account export readiness, independent from cycle completion generations."""

    PENDING = "pending", "Na fila"
    BUILDING = "building", "Preparando arquivo"
    READY = "ready", "Disponível para download"
    FAILED = "failed", "Não foi possível gerar"
    EXPIRED = "expired", "Prazo de download encerrado"


class AccountExport(TenantScopedModel):
    """An owner-authorized account snapshot, never exposed to a portal seat."""

    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    state = models.CharField(
        max_length=16,
        choices=AccountExportState.choices,
        default=AccountExportState.PENDING,
    )
    version = models.PositiveIntegerField(default=1)
    fence = models.PositiveBigIntegerField(default=0)
    lease_until = models.DateTimeField(null=True)
    storage_key = models.CharField(max_length=255, blank=True)
    byte_size = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    captured_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)
    expires_at = models.DateTimeField(null=True)

    class Meta(TenantScopedModel.Meta):
        """Scope the attempt FK and constrain the recoverable state machine."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tenant", "id"], name="account_export_scope_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=AccountExportState.values, version__gt=0),
                name="account_export_known_state",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "state", "created_at"],
                name="account_export_queue_idx",
            ),
        ]


class AccountExportAttempt(TenantScopedModel):
    """The exact owned key survives a worker crash and a failed cleanup."""

    export = models.ForeignKey(AccountExport, on_delete=models.PROTECT)
    fence = models.PositiveBigIntegerField()
    owner = models.UUIDField(default=uuid4)
    storage_key = models.CharField(max_length=255)
    cleanup_pending = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantScopedModel.Meta):
        """Each scoped archive fence has one separately owned storage attempt."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tenant", "export", "fence"], name="account_attempt_fence_uniq"
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "created_at"], name="account_attempt_created_idx"
            ),
        ]
