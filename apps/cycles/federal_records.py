"""The accountant's review of a Receita fiscal-situation report (AXRM-016).

Append-only (trigger for every role, SELECT/INSERT only for app_runtime) and
firm-only (DenyPortalAccess, INV-R10), following the `Review` pattern of
`apps.cycles.evidence`: a later review appends a new row and never rewrites an
earlier decision. `input_digest` pins the exact report bytes the reviewer saw;
`pendencias` is the reviewer's firm-internal list, never copied into an event.
"""

from typing import ClassVar, Final

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.cycles.records import ClientRecord, ServiceCycle

MAX_PENDENCIAS: Final = 50
MAX_PENDENCIA_LENGTH: Final = 500


class FederalReportOutcome(models.TextChoices):
    """What the reviewer recorded; never a "regular" or verified assertion."""

    NADA_REGISTRADO = "sem_pendencias_nas_fontes", "Nada registrado nas fontes"
    PENDENCIAS = "pendencias_registradas", "Pendências registradas"
    ILEGIVEL = "ilegivel", "Relatório ilegível"


class FederalReportReview(ClientRecord):
    """One reviewer decision on one emitted report, inside one cycle."""

    report = models.ForeignKey(
        "obligations.FiscalSituationReport",
        on_delete=models.PROTECT,
        related_name="+",
    )
    cycle = models.ForeignKey(ServiceCycle, on_delete=models.PROTECT, related_name="+")
    outcome = models.CharField(max_length=32, choices=FederalReportOutcome.choices)
    pendencias = models.JSONField(default=list, blank=True)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    reviewed_at = models.DateTimeField(default=timezone.now)
    input_digest = models.CharField(max_length=64)

    class Meta(ClientRecord.Meta):
        """Known outcomes, a digest, and pendencias exactly when they are recorded."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.CheckConstraint(
                condition=Q(outcome__in=FederalReportOutcome.values),
                name="federalreportreview_known_outcome",
            ),
            models.CheckConstraint(
                condition=Q(input_digest__regex="^[a-f0-9]{64}$"),
                name="federalreportreview_input_digest",
            ),
            models.CheckConstraint(
                condition=(
                    Q(outcome=FederalReportOutcome.PENDENCIAS) & ~Q(pendencias=[])
                )
                | (~Q(outcome=FederalReportOutcome.PENDENCIAS) & Q(pendencias=[])),
                name="federalreportreview_pendencias_by_outcome",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            *ClientRecord.Meta.indexes,
            models.Index(
                fields=["tenant", "client", "report"],
                name="federalreview_report_idx",
            ),
        ]
