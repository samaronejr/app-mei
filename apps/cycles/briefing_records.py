"""Firm-private preferences and recorded dismissals for the briefing read model."""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TenantScopedModel
from apps.cycles.records import ClientRecord


class SavedView(TenantScopedModel):
    """A person's named filters, never a saved authorization decision."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="briefing_views",
    )
    name = models.CharField(max_length=80)
    filters_json = models.JSONField(default=dict)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantScopedModel.Meta):
        """One name per person and firm; portal access is explicitly denied."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tenant", "id"], name="savedview_tenant_id_uniq"
            ),
            models.UniqueConstraint(
                fields=["tenant", "user", "name"], name="savedview_user_name_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(version__gt=0), name="savedview_positive_version"
            ),
            models.CheckConstraint(
                condition=~models.Q(name=""), name="savedview_has_name"
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["tenant", "user"], name="savedview_user_idx")
        ]


class BriefingFeedback(ClientRecord):
    """A reasoned dismissal of one fact version, private to the person who made it.

    A newer source version is a different fact and is never hidden by this row.
    Reasons are erasable content, not append-only audit payloads.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="briefing_feedback",
    )
    kind = models.CharField(max_length=24)
    subject_ref = models.UUIDField()
    fact_version = models.CharField(max_length=128)
    reason = models.CharField(max_length=500)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(ClientRecord.Meta):
        """Keep both feedback identity and lookup within the client boundary."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=[
                    "tenant",
                    "client",
                    "user",
                    "kind",
                    "subject_ref",
                    "fact_version",
                ],
                name="briefingfeedback_fact_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gt=0),
                name="briefingfeedback_version_check",
            ),
            models.CheckConstraint(
                condition=~models.Q(reason=""),
                name="briefingfeedback_has_reason",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            *ClientRecord.Meta.indexes,
            models.Index(
                fields=["tenant", "user", "kind", "subject_ref"],
                name="briefingfeedback_user_idx",
            ),
        ]
