"""The accountant's immutable decision on an exact fiscal revision and its bytes."""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.cycles.records import ClientRecord


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

    class Meta(ClientRecord.Meta):
        """A decision belongs to this client's exact revision and monthly cycle."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=("tenant", "client", "revision", "cycle"),
                name="fiscaldocumentreview_revision_uniq",
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
