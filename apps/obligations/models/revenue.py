"""What the client actually billed, one row per competence month.

**Scope limit.** Revenue is entered manually or seeded in this phase. Invoice capture
is Phase 3 and bank import is Phase 4, so this table is deliberately the whole story
about turnover for now, and the threshold monitor says nothing about where the number
came from.

**Uniqueness is a database constraint because the failure is silent.** Without it a
duplicated entry double-counts, and double-counting turnover is the specific error
that pushes a compliant client over the ceiling in the monitor while Receita Federal
sees them comfortably inside it. Nothing about the resulting figure looks wrong.
"""

from typing import ClassVar

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantScopedModel

# BRL, per the anchor. Decimal end to end: binary floating point cannot represent
# 0.20, and a rounding error here moves a desenquadramento date by a full year.
MONEY_DIGITS = 14
MONEY_DECIMAL_PLACES = 2


class MonthlyRevenue(TenantScopedModel):
    """One client's gross turnover for one competence month."""

    client = models.ForeignKey(
        "clients.ClientCompany",
        on_delete=models.CASCADE,
        related_name="monthly_revenues",
        verbose_name=_("client"),
    )
    competence_month = models.DateField(_("competence month"))
    gross_amount = models.DecimalField(
        _("gross amount"),
        max_digits=MONEY_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        verbose_name = _("monthly revenue")
        verbose_name_plural = _("monthly revenues")
        ordering: ClassVar[list[str]] = ["competence_month"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tenant", "client", "competence_month"],
                name="monthlyrevenue_tenant_client_competence_uniq",
            ),
            # Two spellings of "June 2026" would each satisfy the uniqueness above
            # and would then both be summed.
            models.CheckConstraint(
                condition=models.Q(competence_month__day=1),
                name="monthlyrevenue_competence_is_month_start",
            ),
            # Turnover is not negative. A refund is a smaller month, not a negative
            # one, and a negative row would quietly lower the accumulated total and
            # hide an overage.
            models.CheckConstraint(
                condition=models.Q(gross_amount__gte=0),
                name="monthlyrevenue_gross_amount_not_negative",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            # tenant_id leads, so the row-level-security predicate stays index-served.
            models.Index(
                fields=["tenant", "competence_month"],
                name="revenue_tenant_competence_idx",
            ),
        ]

    def __str__(self) -> str:
        """Identify the entry by its period and amount."""
        return f"{self.competence_month:%Y-%m} R$ {self.gross_amount}"
