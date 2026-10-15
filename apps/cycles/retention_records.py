"""Private legal holds and durable deletion claims serialized by the client."""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.cycles.records import ClientRecord


class LegalHold(ClientRecord):
    """A client-wide hold covers every engagement and shared original."""

    reason = models.TextField()
    placed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    placed_at = models.DateTimeField(default=timezone.now)
    released_at = models.DateTimeField(null=True)
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", null=True
    )
    version = models.PositiveIntegerField(default=1)


class ContentPurgeClaim(ClientRecord):
    """A committed claim is the irreversible deletion authorization point."""

    content_kind = models.CharField(max_length=32)
    content_id = models.UUIDField()
    storage_keys = models.JSONField(default=list)
    completed_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(default=timezone.now)
    error_code = models.CharField(max_length=64, blank=True)

    class Meta(ClientRecord.Meta):
        """Prevent duplicate deletion claims for the same scoped content."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "client", "content_kind", "content_id"],
                name="contentpurge_identity_uniq",
            ),
        ]
