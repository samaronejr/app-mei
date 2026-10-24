"""Private monthly budgets and the individual exposures making up their balances."""

from typing import ClassVar

from django.db import models

from apps.core.models import TenantScopedModel
from apps.cycles.records import ClientRecord


class ProviderBudget(TenantScopedModel):
    """An explicit monthly authorization, never implicitly carried into next month."""

    provider = models.CharField(
        max_length=16, choices=(("serpro", "SERPRO"), ("focus", "Focus"))
    )
    window_start = models.DateField()
    cap_microbrl = models.PositiveBigIntegerField()
    known_liability = models.PositiveBigIntegerField(default=0)
    uncertain_exposure = models.PositiveBigIntegerField(default=0)
    reserved = models.PositiveBigIntegerField(default=0)
    tariff_revision = models.CharField(max_length=64)
    included_documents = models.PositiveIntegerField(default=0)
    version = models.PositiveIntegerField(default=1)

    class Meta(TenantScopedModel.Meta):
        """The same provider shares one ceiling across all environments and clients."""

        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=("tenant", "provider"), name="providerbudget_scope_idx")
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("tenant", "provider", "window_start"),
                name="providerbudget_window_uniq",
            ),
            models.UniqueConstraint(
                fields=("tenant", "id"), name="providerbudget_tenant_id_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(window_start__day=1),
                name="providerbudget_month_start",
            ),
            models.CheckConstraint(
                condition=models.Q(provider__in=("serpro", "focus")),
                name="providerbudget_known_provider",
            ),
        ]


class ProviderReservation(ClientRecord):
    """One attempt's balance; replay moves its bucket rather than charging twice."""

    budget = models.ForeignKey(
        ProviderBudget, on_delete=models.PROTECT, related_name="+"
    )
    call = models.ForeignKey(
        "integrations.ProviderCall", on_delete=models.PROTECT, related_name="+"
    )
    attempt_number = models.PositiveIntegerField(default=1)
    microbrl = models.PositiveBigIntegerField(default=0)
    state = models.CharField(
        max_length=16,
        choices=(
            ("reserved", "Reserved"),
            ("known", "Known"),
            ("uncertain", "Uncertain"),
            ("released", "Released"),
        ),
        default="reserved",
    )
    tariff_revision = models.CharField(max_length=64)
    version = models.PositiveIntegerField(default=1)

    class Meta(ClientRecord.Meta):
        """Each transmission owns one reservation, including read retries."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=("tenant", "client", "call", "attempt_number"),
                name="providerreservation_attempt_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    state__in=("reserved", "known", "uncertain", "released")
                ),
                name="providerreservation_known_state",
            ),
        ]
