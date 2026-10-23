"""Private protocol identities, immutable attempts, effect barriers and epoch."""

from typing import ClassVar

from django.db import models
from django.utils import timezone

from apps.cycles.records import ClientRecord


class CallState(models.TextChoices):
    """External execution certainty stays separate from fiscal verification."""

    QUEUED = "na_fila", "Na fila"
    RESERVED = "reservada", "Reservada"
    CLAIMED = "reivindicada", "Reivindicada"
    STARTED = "despacho_iniciado", "Despacho iniciado"
    SUCCESS = "sucesso", "Sucesso"
    WAITING = "aguardando_resultado", "Aguardando resultado"
    RETRY = "aguardar_nova_tentativa", "Aguardar nova tentativa"
    FAILED = "falhou", "Falhou"
    DENIED = "negada", "Negada"
    UNCERTAIN = "incerta", "Incerta"
    RECONCILED = "reconciliada", "Reconciliada"
    QUOTA = "bloqueada_cota", "Bloqueada por cota"
    AUTHORITY = "bloqueada_autoridade", "Bloqueada por autoridade"
    PAUSED = "pausada", "Pausada"
    CANCELLED = "cancelada", "Cancelada"


class ProviderCall(ClientRecord):
    """One canonical client request; state changes use short fenced transactions."""

    link = models.ForeignKey(
        "integrations.ClientProviderLink", on_delete=models.PROTECT, related_name="+"
    )
    operation = models.CharField(max_length=24)
    operation_revision = models.CharField(max_length=32)
    operation_class = models.CharField(max_length=16)
    provider = models.CharField(max_length=16)
    environment = models.CharField(max_length=16)
    request_key = models.CharField(max_length=64)
    business_effect_key = models.CharField(max_length=64)
    request_digest = models.CharField(max_length=64)
    state = models.CharField(
        max_length=32, choices=CallState.choices, default=CallState.QUEUED
    )
    version = models.PositiveIntegerField(default=1)
    fence = models.PositiveBigIntegerField(default=0)
    lease_owner = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    execution_epoch = models.PositiveBigIntegerField(default=1)
    attempts = models.PositiveIntegerField(default=0)
    retry_of = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, related_name="+"
    )
    next_attempt_at = models.DateTimeField(default=timezone.now)
    dispatch_started_at = models.DateTimeField(null=True)
    finalized_at = models.DateTimeField(null=True)
    connection_version = models.PositiveIntegerField(default=1)
    link_version = models.PositiveIntegerField(default=1)
    credential_generation = models.PositiveIntegerField(default=0)
    http_status = models.PositiveSmallIntegerField(null=True)
    provider_code = models.CharField(max_length=64, blank=True)
    billing_class = models.CharField(max_length=24)
    billed = models.CharField(max_length=16, default="desconhecido")
    reserved_microbrl = models.PositiveBigIntegerField(default=0)
    settled_microbrl = models.PositiveBigIntegerField(null=True)
    tariff_revision = models.CharField(max_length=64, blank=True)
    response_digest = models.CharField(max_length=64, blank=True)
    x_request_tag = models.UUIDField(null=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta(ClientRecord.Meta):
        """Request identity, retries and every parent remain private to the client."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=("tenant", "client", "request_key"),
                name="providercall_request_uniq",
            ),
            models.UniqueConstraint(
                fields=("tenant", "client", "retry_of"),
                name="providercall_retry_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=CallState.values),
                name="providercall_known_state",
            ),
            models.CheckConstraint(
                condition=models.Q(billed__in=("sim", "nao", "desconhecido")),
                name="providercall_known_billed",
            ),
        ]


class ProviderCallAttempt(ClientRecord):
    """Append-only per-attempt observations; stale replies never rewrite the call."""

    call = models.ForeignKey(ProviderCall, on_delete=models.PROTECT, related_name="+")
    attempt_id = models.UUIDField()
    attempt_number = models.PositiveIntegerField(default=1)
    fence = models.PositiveBigIntegerField(default=0)
    outcome = models.CharField(max_length=32)
    observation = models.CharField(max_length=24, default="result")
    http_status = models.PositiveSmallIntegerField(null=True)
    provider_code = models.CharField(max_length=64, blank=True)
    billed = models.CharField(max_length=16, default="desconhecido")
    response_digest = models.CharField(max_length=64, blank=True)
    reserved_microbrl = models.PositiveBigIntegerField(default=0)
    settled_microbrl = models.PositiveBigIntegerField(null=True)
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta(ClientRecord.Meta):
        """Replaying the same observation cannot meter it twice."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=("tenant", "client", "call", "attempt_id", "observation"),
                name="providerattempt_observation_uniq",
            ),
        ]


class ProviderEffectBarrier(ClientRecord):
    """Block repeated issuance, registration and configuration business effects."""

    provider = models.CharField(max_length=16)
    business_effect_key = models.CharField(max_length=64)
    request_digest = models.CharField(max_length=64)
    first_start_receipt = models.CharField(max_length=255)
    call = models.ForeignKey(
        ProviderCall, on_delete=models.PROTECT, null=True, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(ClientRecord.Meta):
        """One irreversible start per client/provider business effect."""

        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=("tenant", "client"), name="providerbarrier_scope_idx"),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=("tenant", "client", "provider", "business_effect_key"),
                name="providerbarrier_effect_uniq",
            ),
        ]


class ProviderExecutionEpoch(models.Model):
    """Platform singleton without tenant data; no portal grant (A7.1)."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    epoch = models.PositiveBigIntegerField(default=1)
    incremented_at = models.DateTimeField(default=timezone.now)
    reason = models.CharField(max_length=24, default="initial")
    restore_ref = models.UUIDField(null=True)

    class Meta:
        """The restore procedure alone may advance the single execution epoch."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(condition=models.Q(id=1), name="provider_epoch_one"),
        ]

    def __str__(self) -> str:
        """Identify the platform generation, never any tenant content."""
        return f"Provider epoch {self.epoch}"
