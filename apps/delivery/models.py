"""Private durable messages; immutable receipts contain only references and codes."""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TenantScopedModel
from apps.core.retention import RetentionClass
from apps.cycles.records import ClientRecord


class MessageState(models.TextChoices):
    """SMTP acceptance is not inbox delivery or a staff assertion."""

    QUEUED = "na_fila", "Na fila"
    SENDING = "enviando", "Enviando"
    ACCEPTED = "aceita_pelo_provedor", "Aceita pelo servidor de e-mail"
    TEMPORARY = "falhou_temporario", "Falha temporária"
    REJECTED = "recusada_pelo_provedor", "Recusada pelo servidor de e-mail"
    UNKNOWN = "resultado_incerto", "Resultado incerto"
    CANCELLED = "cancelada", "Cancelada"
    MANUAL_SENT = "informada_enviada", "Envio informado pela equipe"
    MANUAL_NOT_SENT = "informada_nao_enviada", "Não envio informado pela equipe"


class OutboundMessage(ClientRecord):
    """One recipient and one immutable content digest per business intent."""

    contact = models.ForeignKey("clients.ClientContact", on_delete=models.PROTECT)
    cycle = models.ForeignKey(
        "cycles.ServiceCycle", on_delete=models.PROTECT, null=True, blank=True
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    # T11: the portal invitation a `portal_invite` message delivers, or the one a
    # request notice embeds for a contact without a seat. The token is minted at
    # dispatch and never stored here; this is only the reference.
    invite = models.ForeignKey(
        "tenants.Invite", on_delete=models.PROTECT, null=True, blank=True
    )
    # T11: the request a reminder is about, re-read by the pre-send hook.
    evidence_request = models.ForeignKey(
        "cycles.EvidenceRequest", on_delete=models.PROTECT, null=True, blank=True
    )
    proposal_id = models.UUIDField(null=True, blank=True)
    kind = models.SlugField(max_length=64, default="notice")
    idempotency_key = models.CharField(max_length=64)
    business_intent_key = models.CharField(max_length=64)
    content_digest = models.CharField(max_length=64)
    recipients_json = models.JSONField(default=list)
    subject = models.CharField(max_length=255)
    body_ref = models.CharField(max_length=255)
    portal_url = models.URLField(max_length=500)
    state = models.CharField(
        max_length=32, choices=MessageState.choices, default=MessageState.QUEUED
    )
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_error_code = models.CharField(max_length=64, blank=True)
    provider_message_id = models.CharField(max_length=255, blank=True)
    lease_owner = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    fence = models.PositiveBigIntegerField(default=0)
    send_started_at = models.DateTimeField(null=True)
    version = models.PositiveIntegerField(default=1)
    decision_note = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)
    retention_class = models.CharField(
        max_length=24,
        choices=RetentionClass.choices,
        default=RetentionClass.COMMUNICATION_BODY,
        db_default=RetentionClass.COMMUNICATION_BODY,
    )
    redacted_at = models.DateTimeField(null=True)

    class Meta(ClientRecord.Meta):
        """Both caller and natural keys are private to the client."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "client", "business_intent_key"],
                name="outbound_intent_uniq",
            ),
            models.UniqueConstraint(
                fields=["tenant", "client", "idempotency_key"],
                name="outbound_idempotency_uniq",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            *ClientRecord.Meta.indexes,
            models.Index(
                fields=["tenant", "state", "next_attempt_at"], name="outbound_queue_idx"
            ),
        ]


class DeliveryAttempt(ClientRecord):
    """Append-only state observations, including stale and duplicate replies."""

    message = models.ForeignKey(OutboundMessage, on_delete=models.PROTECT)
    attempt_id = models.UUIDField(null=True)
    fence = models.PositiveBigIntegerField(default=0)
    outcome = models.CharField(max_length=32)
    error_code = models.CharField(max_length=64, blank=True)
    actor_id = models.UUIDField(null=True)
    note_digest = models.CharField(max_length=64, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)


class DeliveryEffect(ClientRecord):
    """Append-only effect barrier, including intents predating the restored DB."""

    message = models.ForeignKey(OutboundMessage, on_delete=models.PROTECT, null=True)
    business_intent_key = models.CharField(max_length=64)
    content_digest = models.CharField(max_length=64)
    outcome = models.CharField(max_length=32)
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta(ClientRecord.Meta):
        """Exactly one external acceptance per business intent and message."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "client", "business_intent_key"],
                name="delivery_effect_intent_uniq",
            ),
            models.UniqueConstraint(
                fields=["tenant", "client", "message"], name="delivery_effect_msg_uniq"
            ),
        ]


class AutomationPause(TenantScopedModel):
    """Tenant kill switch and serialized conservative daily send reservations."""

    paused = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    quota_day = models.DateField(default=timezone.localdate)
    reserved_today = models.PositiveIntegerField(default=0)

    class Meta(TenantScopedModel.Meta):
        """One dispatcher coordination row per firm."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tenant"], name="delivery_pause_tenant_uniq"
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["tenant", "quota_day"], name="delivery_pause_day_idx"),
        ]
