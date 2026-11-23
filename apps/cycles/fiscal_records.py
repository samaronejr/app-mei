"""The accountant's immutable decision on an exact fiscal revision and its bytes."""

from typing import ClassVar, Final

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from apps.cycles.fiscal_review_reason import REVIEW_REASON_NONBLANK_V1
from apps.cycles.records import ClientRecord

DECISIONS: Final = (
    ("aceito", "Aceito pela equipe"),
    ("rejeitado", "Não aceito"),
    ("requer_xml", "Requer XML completo"),
    ("documento_de_terceiro", "Documento de terceiro"),
)


class FiscalDocumentReview(ClientRecord):
    """A later revision needs another decision; a past decision never changes."""

    document = models.ForeignKey(
        "obligations.FiscalDocument", on_delete=models.PROTECT, related_name="+"
    )
    revision = models.ForeignKey(
        "obligations.FiscalRevision", on_delete=models.PROTECT, related_name="+"
    )
    cycle = models.ForeignKey(
        "cycles.ServiceCycle", on_delete=models.PROTECT, related_name="+"
    )
    supersedes = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        db_constraint=False,  # The migration installs the stronger exact-chain FK.
    )
    decision = models.CharField(
        max_length=24,
        choices=[
            (value, value)
            for value in ("aceito", "rejeitado", "requer_xml", "documento_de_terceiro")
        ],
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    input_digest = models.CharField(max_length=64)
    note = models.TextField(blank=True, max_length=2000)
    reviewed_at = models.DateTimeField(default=timezone.now)
    version = models.PositiveIntegerField(default=1)

    @property
    def decision_label(self) -> str:
        """Show the same label in the form and in the immutable history."""
        return dict(DECISIONS)[self.decision]

    class Meta(ClientRecord.Meta):
        """A decision belongs to this client's exact revision and monthly cycle."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=("tenant", "client", "revision", "cycle", "supersedes"),
                nulls_distinct=False,
                name="fiscaldocumentreview_revision_uniq",
            ),
            models.UniqueConstraint(
                fields=("tenant", "client", "revision", "cycle", "id"),
                name="fiscaldocumentreview_chain_id_uniq",
            ),
            models.CheckConstraint(
                condition=~Q(supersedes=F("id")),
                name="fiscaldocumentreview_not_self",
            ),
            models.CheckConstraint(
                condition=Q(supersedes__isnull=True)
                | Q(note__regex=REVIEW_REASON_NONBLANK_V1),
                name="fiscaldocumentreview_successor_reason",
            ),
            models.CheckConstraint(
                condition=Q(
                    decision__in=[
                        "aceito",
                        "rejeitado",
                        "requer_xml",
                        "documento_de_terceiro",
                    ]
                ),
                name="fiscaldocumentreview_known_decision",
            ),
            models.CheckConstraint(
                condition=Q(input_digest__regex="^[a-f0-9]{64}$"),
                name="fiscaldocumentreview_input_digest",
            ),
        ]
