"""Firm-private rehearsal snapshots and fenced, independently committed items."""

from typing import ClassVar, Final

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TenantScopedModel
from apps.cycles.records import ClientRecord


class BulkKind(models.TextChoices):
    """The four bounded R1 operations; none performs external I/O."""

    OPEN_CYCLES = "open_cycles", "Abrir ciclos"
    SEND_REQUEST_NOTICES = "send_request_notices", "Enviar avisos de solicitações"
    MIGRATE_OPEN_CYCLES = "migrate_open_cycles_to_version", "Migrar ciclos abertos"
    APPROVE_ITEMS = "approve_items", "Aprovar efeitos por item"


class BulkOperationState(models.TextChoices):
    """A completed batch is not necessarily a successful batch."""

    PREVIEW = "preview", "Prévia"
    QUEUED = "queued", "Na fila"
    RUNNING = "running", "Em andamento"
    CANCELLING = "cancelling", "Cancelando"
    CANCELLED = "cancelled", "Cancelado"
    COMPLETED = "completed", "Concluído"
    COMPLETED_WITH_FAILURES = "completed_with_failures", "Concluído com falhas"


class BulkItemState(models.TextChoices):
    """A skipped or cancelled item is never counted as an executed effect."""

    PENDING = "pending", "Pendente"
    CLAIMED = "claimed", "Em processamento"
    DONE = "done", "Processado"
    SKIPPED_CHANGED = "skipped_changed", "Ignorado: registro alterado"
    SKIPPED_FORBIDDEN = "skipped_forbidden", "Ignorado: acesso revogado"
    SKIPPED_INELIGIBLE = "skipped_ineligible", "Não elegível"
    SKIPPED_EXCLUDED = "skipped_excluded", "Excluído da execução"
    CANCELLED = "cancelled", "Cancelado sem executar"
    FAILED = "failed", "Falhou"


ACTIVE_OPERATIONS: Final = (
    BulkOperationState.QUEUED,
    BulkOperationState.RUNNING,
    BulkOperationState.CANCELLING,
)
UNFINISHED_ITEMS: Final = (BulkItemState.PENDING, BulkItemState.CLAIMED)
BULK_CAPABILITY: Final = "bulk.run"


class BulkOperation(TenantScopedModel):
    """An immutable preview, followed by versioned confirmation and controls."""

    kind = models.CharField(max_length=32, choices=BulkKind.choices)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="bulk_operations",
    )
    snapshot_json = models.JSONField()
    snapshot_digest = models.CharField(max_length=64)
    valid_until = models.DateTimeField()
    state = models.CharField(
        max_length=24,
        choices=BulkOperationState.choices,
        default=BulkOperationState.PREVIEW,
    )
    idempotency_key = models.CharField(max_length=64)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)
    confirmed_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)

    class Meta(TenantScopedModel.Meta):
        """Tenant-paired item references and a small recovery queue."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tenant", "id"], name="bulkoperation_scope_id_uniq"
            ),
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"],
                name="bulkoperation_intent_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=BulkOperationState.values),
                name="bulkoperation_known_state",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gt=0),
                name="bulkoperation_positive_version",
            ),
            models.CheckConstraint(
                condition=models.Q(valid_until__gt=models.F("created_at")),
                name="bulkoperation_preview_interval",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "state", "created_at"], name="bulkoperation_queue_idx"
            ),
        ]


class BulkItem(ClientRecord):
    """A frozen subject and effect key, with a leased monotonic execution fence."""

    operation = models.ForeignKey(
        BulkOperation, on_delete=models.PROTECT, related_name="items"
    )
    subject_ref = models.CharField(max_length=64)
    expected_version = models.PositiveIntegerField()
    effect_key = models.CharField(max_length=64)
    position = models.PositiveIntegerField()
    state = models.CharField(
        max_length=24, choices=BulkItemState.choices, default=BulkItemState.PENDING
    )
    result_json = models.JSONField(default=dict)
    fence = models.PositiveIntegerField(default=0)
    lease_owner = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)

    class Meta(ClientRecord.Meta):
        """The claim identity includes both security boundaries and the snapshot."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "client", "operation", "subject_ref"],
                name="bulkitem_subject_uniq",
            ),
            models.UniqueConstraint(
                fields=["tenant", "client", "effect_key"], name="bulkitem_effect_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=BulkItemState.values),
                name="bulkitem_known_state",
            ),
            models.CheckConstraint(
                condition=models.Q(expected_version__gt=0),
                name="bulkitem_positive_expectation",
            ),
            models.CheckConstraint(
                condition=~models.Q(state=BulkItemState.CLAIMED)
                | models.Q(
                    lease_owner__isnull=False, lease_until__isnull=False, fence__gt=0
                ),
                name="bulkitem_claim_has_fence",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            *ClientRecord.Meta.indexes,
            models.Index(
                fields=["tenant", "operation", "state"], name="bulkitem_resume_idx"
            ),
        ]
