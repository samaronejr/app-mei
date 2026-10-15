"""Private shadow receipts and platform-only synthetic evaluation metrics."""

from typing import ClassVar

from django.db import models
from django.utils import timezone

from apps.automation.types import RunState
from apps.core.models import UUIDv7PrimaryKeyModel
from apps.cycles.records import ClientRecord


class AgentRun(ClientRecord):
    """Reserve once, finalize once; content expires separately from cost metadata.

    Pending reservations survive interruption, so redelivery cannot spend again.
    Prompt/document text is never stored. Output alone is ai_derived content.
    """

    document = models.ForeignKey(
        "obligations.Document", on_delete=models.SET_NULL, null=True, related_name="+"
    )
    input_hash = models.CharField(max_length=64)
    prompt_version = models.CharField(max_length=32, default="doc_classify_v1")
    model_id = models.CharField(max_length=128)
    state = models.CharField(max_length=32, default=RunState.PENDING)
    output = models.JSONField(default=dict)
    error_code = models.CharField(max_length=64, blank=True)
    # Sub-cent accounting must not round a provider call down to free.
    reserved_brl = models.DecimalField(max_digits=14, decimal_places=6)
    cost_brl = models.DecimalField(max_digits=14, decimal_places=6, null=True)
    currency = models.CharField(max_length=3, default="BRL")
    latency_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    finalized_at = models.DateTimeField(null=True)
    expires_at = models.DateTimeField()
    retention_class = models.CharField(max_length=16, default="ai_derived")

    class Meta(ClientRecord.Meta):
        """Indexed admission and expiry under forced tenant RLS."""

        indexes: ClassVar[list[models.Index]] = [
            *ClientRecord.Meta.indexes,
            models.Index(fields=["tenant", "created_at"], name="agent_budget_idx"),
            models.Index(
                fields=["tenant", "client", "input_hash"], name="agent_source_idx"
            ),
            models.Index(fields=["tenant", "expires_at"], name="agent_expiry_idx"),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.CheckConstraint(
                condition=models.Q(reserved_brl__gte=0),
                name="agent_reserve_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(cost_brl__isnull=True) | models.Q(cost_brl__gte=0),
                name="agent_cost_nonnegative",
            ),
        ]


class EvalRun(UUIDv7PrimaryKeyModel):
    """Platform metrics for synthetic documents only; no tenant/client content."""

    model_id = models.CharField(max_length=128)
    prompt_version = models.CharField(max_length=32, default="doc_classify_v1")
    dataset_version = models.CharField(max_length=32)
    dataset_hash = models.CharField(max_length=64)
    metrics = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now)
