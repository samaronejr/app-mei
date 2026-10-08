"""Private firm notifications do not borrow a client's identity or contact."""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TenantScopedModel


class NotificationPreference(TenantScopedModel):
    """One person's opt-in inside one firm, edited with a rendered revision."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    daily_summary = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantScopedModel.Meta):
        """A membership in another firm does not share this preference."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tenant", "user"], name="notification_preference_user_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(version__gt=0), name="notification_positive_version"
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "daily_summary"], name="notification_summary_idx"
            ),
        ]


class FirmMessageKind(models.TextChoices):
    """Each purpose resolves its recipient from an authorized reference."""

    OWNER_INVITE = "owner_invite", "Convite ao responsável"
    DAILY_SUMMARY = "daily_summary", "Resumo diário"


class FirmMessageState(models.TextChoices):
    """Provider acceptance never claims delivery to an inbox."""

    QUEUED = "queued", "Na fila"
    SENDING = "sending", "Enviando"
    ACCEPTED = "provider_accepted", "Aceita pelo servidor de e-mail"
    FAILED = "failed", "Falha no envio"
    UNKNOWN = "outcome_unknown", "Resultado incerto"
    CANCELLED = "cancelled", "Cancelada"


class FirmOutboundMessage(TenantScopedModel):
    """A durable intent; tokens, recipients and rendered bodies are not persisted."""

    kind = models.CharField(max_length=24, choices=FirmMessageKind.choices)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT
    )
    invite = models.ForeignKey(
        "tenants.Invite", null=True, blank=True, on_delete=models.PROTECT
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    period = models.DateField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=64)
    state = models.CharField(
        max_length=24, choices=FirmMessageState.choices, default=FirmMessageState.QUEUED
    )
    version = models.PositiveIntegerField(default=1)
    fence = models.PositiveBigIntegerField(default=0)
    attempt_id = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    last_error_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True)

    class Meta(TenantScopedModel.Meta):
        """One purpose/recipient reference and one outbox row per business intent."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"], name="firm_message_intent_uniq"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        kind=FirmMessageKind.OWNER_INVITE,
                        invite__isnull=False,
                        user__isnull=True,
                        period__isnull=True,
                        created_by__isnull=False,
                    )
                    | models.Q(
                        kind=FirmMessageKind.DAILY_SUMMARY,
                        invite__isnull=True,
                        user__isnull=False,
                        period__isnull=False,
                    )
                ),
                name="firm_message_recipient_shape",
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=FirmMessageState.values, version__gt=0),
                name="firm_message_state_version",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "state", "created_at"], name="firm_message_queue_idx"
            ),
        ]
