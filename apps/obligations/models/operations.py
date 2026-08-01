"""What this deployment scheduled: the generated obligations and beat's pulse.

Distinct from the reference tables next door. Those record what the law requires;
these record what this installation has actually produced and whether the process
that produces it is still running.
"""

from typing import ClassVar

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantScopedModel
from apps.obligations.models.reference import ObligationType


class SchedulerHeartbeat(models.Model):
    """Proof that the scheduler is still running, written on a timer.

    Platform-level: there is one beat process for the whole deployment, not one per
    firm, and a per-tenant heartbeat would say nothing about the process itself.

    This exists because the scheduler's failure mode is SILENCE. If the beat
    container stops, no task raises, no obligation is generated, and no error reaches
    Sentry — nothing failed, nothing happened at all. The only way to detect that is
    to require a positive signal and alarm on its absence.
    """

    name = models.CharField(_("name"), max_length=64, unique=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)
    # Only the host-side writer populates this: `ops/backup.sh` is the one signal that
    # runs OUTSIDE a container and can therefore see the host filesystem at all. Beat
    # leaves it null, and so does a marker written before this column existed — which
    # is why it is nullable and why null reads as "unknown" rather than as "full".
    free_disk_kib = models.BigIntegerField(
        _("free disk KiB"),
        null=True,
        blank=True,
    )

    class Meta:
        """Model metadata."""

        verbose_name = _("scheduler heartbeat")
        verbose_name_plural = _("scheduler heartbeats")
        ordering: ClassVar[list[str]] = ["name"]

    def __str__(self) -> str:
        """Identify the heartbeat by who wrote it and when."""
        return f"{self.name} @ {self.updated_at.isoformat()}"


class ObligationStatus(models.TextChoices):
    """Where one obligation sits in its life."""

    SCHEDULED = "scheduled", _("scheduled")
    DUE = "due", _("due")
    PAID = "paid", _("paid")
    OVERDUE = "overdue", _("overdue")
    WAIVED = "waived", _("waived")


class Obligation(TenantScopedModel):
    """One filing or payment a client owes for one competence period.

    Both dates are stored. `nominal_due_date` is what the rule places and is stable;
    `resolved_due_date` is what the client must actually pay by and moves if the
    holiday table is later corrected. Keeping only the resolved one would make a
    calendar correction indistinguishable from a rule change after the fact.
    """

    client = models.ForeignKey(
        "clients.ClientCompany",
        on_delete=models.CASCADE,
        related_name="obligations",
        verbose_name=_("client"),
    )
    obligation_type = models.ForeignKey(
        ObligationType,
        on_delete=models.PROTECT,
        related_name="obligations",
        verbose_name=_("obligation type"),
    )
    # The first day of the competence month, which a CHECK constraint enforces so
    # that two spellings of "June 2026" cannot both exist and defeat the uniqueness
    # that makes generation idempotent.
    competence_month = models.DateField(_("competence month"))
    nominal_due_date = models.DateField(_("nominal due date"))
    resolved_due_date = models.DateField(_("resolved due date"))
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=ObligationStatus.choices,
        default=ObligationStatus.SCHEDULED,
    )
    # A pointer, never a payload. The document vault is Phase 2, and a column that
    # could hold the receipt itself would quietly make this product a custodian of
    # every firm's fiscal evidence a wave before that decision is due.
    evidence_ref = models.CharField(
        _("evidence reference"),
        max_length=255,
        blank=True,
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        verbose_name = _("obligation")
        verbose_name_plural = _("obligations")
        ordering: ClassVar[list[str]] = ["competence_month"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # Idempotency as a DATABASE constraint, not a convention. acks_late means
            # a task that loses its worker is redelivered, so two generators can be
            # in flight at once and both find nothing when they check.
            models.UniqueConstraint(
                fields=["tenant", "client", "obligation_type", "competence_month"],
                name="obligation_tenant_client_type_competence_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(competence_month__day=1),
                name="obligation_competence_is_month_start",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            # tenant_id leads. The row-level-security predicate filters on it, and an
            # index that does not lead with it stops serving the predicate.
            models.Index(
                fields=["tenant", "resolved_due_date"],
                name="obligation_tenant_due_idx",
            ),
            models.Index(
                fields=["tenant", "status"],
                name="obligation_tenant_status_idx",
            ),
        ]

    def __str__(self) -> str:
        """Identify the obligation by what it is and which period it covers."""
        return f"{self.obligation_type_id} {self.competence_month:%Y-%m}"
