"""Tenant-only work and explicit zero-cost reservations for firm SP-TERMO calls."""

from typing import ClassVar, Final

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import TenantScopedModel

ACTIVE_STATES: Final = ("aberta", "em_tratamento")


class FirmIntegrationIssue(TenantScopedModel):
    """Owned firm work without inventing a taxpayer for a firm-level authorization."""

    connection = models.ForeignKey(
        "integrations.ProviderConnection", on_delete=models.PROTECT, related_name="+"
    )
    call = models.ForeignKey(
        "integrations.FirmProviderCall",
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )
    reason = models.CharField(max_length=40)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    state = models.CharField(
        max_length=16,
        default="aberta",
        choices=[
            ("aberta", "Aberta"),
            ("em_tratamento", "Em tratamento"),
            ("resolvida", "Resolvida"),
        ],
    )
    review_on = models.DateField()
    evidence_refs = ArrayField(
        models.CharField(max_length=64), default=list, blank=True
    )
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantScopedModel.Meta):
        """Exactly one active condition per firm connection."""

        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=("tenant", "state"), name="firmissue_scope_idx")
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("tenant", "connection", "reason"),
                condition=Q(state__in=ACTIVE_STATES),
                name="firmissue_active_uniq",
            ),
        ]


class FirmProviderReservation(TenantScopedModel):
    """A real admission record for a free attempt, never a fabricated client balance."""

    call = models.ForeignKey(
        "integrations.FirmProviderCall", on_delete=models.PROTECT, related_name="+"
    )
    budget = models.ForeignKey(
        "integrations.ProviderBudget", on_delete=models.PROTECT, related_name="+"
    )
    attempt_number = models.PositiveIntegerField()
    microbrl = models.PositiveBigIntegerField(default=0)
    tariff_revision = models.CharField(max_length=64, default="serpro-free-v1")
    state = models.CharField(
        max_length=16,
        default="reserved",
        choices=[("reserved", "Reserved"), ("released", "Released")],
    )
    version = models.PositiveIntegerField(default=1)

    class Meta(TenantScopedModel.Meta):
        """One explicit zero reservation per client-less transmission."""

        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=("tenant", "call"), name="firmreservation_scope_idx")
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("tenant", "call", "attempt_number"),
                name="firmreservation_attempt_uniq",
            ),
            models.CheckConstraint(
                condition=Q(microbrl=0), name="firmreservation_is_free"
            ),
            models.CheckConstraint(
                condition=Q(state__in=("reserved", "released")),
                name="firmreservation_known_state",
            ),
        ]
