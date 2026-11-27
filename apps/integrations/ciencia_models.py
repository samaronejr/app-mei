"""Private per-key Ciencia identity and duty, independent of approvals and cycles."""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.cycles.records import ClientRecord


class CienciaRecord(ClientRecord):
    """One registered target and its owned duty, even after a pre-approval restore."""

    call = models.ForeignKey(
        "integrations.ProviderCall",
        on_delete=models.PROTECT,
        related_name="+",
    )
    read_call = models.ForeignKey(
        "integrations.ProviderCall",
        on_delete=models.PROTECT,
        null=True,
        related_name="+",
    )
    environment = models.CharField(max_length=16)
    document_key = models.CharField(max_length=44)
    proposal_ref = models.UUIDField()
    cycle_ref = models.UUIDField()
    fiscal_revision_ref = models.UUIDField()
    engagement_ref = models.UUIDField()
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    issued_on = models.DateField()
    authorized_on = models.DateField(null=True)
    annex_ii = models.BooleanField(default=False)
    confirmed_at = models.DateTimeField(null=True)
    verified_at = models.DateTimeField(null=True)
    read_sequence = models.PositiveIntegerField(default=0)
    cycle_exception = models.ForeignKey(
        "cycles.Exception",
        on_delete=models.PROTECT,
        null=True,
        related_name="+",
    )
    issue = models.ForeignKey(
        "integrations.IntegrationIssue",
        on_delete=models.PROTECT,
        null=True,
        related_name="+",
    )
    resolution = models.JSONField(null=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        related_name="+",
    )
    resolved_at = models.DateTimeField(null=True)
    recovered = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(ClientRecord.Meta):
        """Exactly one per-key identity; parent scopes remain database enforced."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=("tenant", "client", "call"),
                name="cienciarecord_call_uniq",
            ),
            models.UniqueConstraint(
                fields=("tenant", "client", "environment", "document_key"),
                name="cienciarecord_key_uniq",
            ),
        ]
