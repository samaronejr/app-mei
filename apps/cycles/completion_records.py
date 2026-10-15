"""Immutable closure proofs with separately erasable justification text."""

from typing import ClassVar

from django.db import models
from django.utils import timezone

from apps.cycles.records import ClientRecord


class CompletionGeneration(ClientRecord):
    """The exact facts and execution of one completed amendment."""

    cycle = models.ForeignKey(
        "cycles.ServiceCycle", on_delete=models.PROTECT, related_name="completions"
    )
    proposal = models.ForeignKey("cycles.ActionProposal", on_delete=models.PROTECT)
    index = models.ForeignKey("cycles.EvidenceIndex", on_delete=models.PROTECT)
    amendment = models.PositiveIntegerField()
    cycle_revision = models.PositiveIntegerField()
    snapshot = models.JSONField()
    digest = models.CharField(max_length=64)
    completed_at = models.DateTimeField(default=timezone.now)

    class Meta(ClientRecord.Meta):
        """Freeze one completion generation for each client-scoped amendment."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "client", "cycle", "amendment"],
                name="completion_generation_uniq",
            ),
        ]


class CompletionJustification(ClientRecord):
    """Private, retention-governed reasons keyed to exact missing item revisions."""

    generation = models.ForeignKey(CompletionGeneration, on_delete=models.PROTECT)
    reasons = models.JSONField(default=dict)
    redacted_at = models.DateTimeField(null=True)

    class Meta(ClientRecord.Meta):
        """Keep one private explanation beside each immutable generation."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "client", "generation"],
                name="completionjustification_once",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "client"], name="completionreason_scope_idx"
            ),
        ]
