"""Public frozen content and separate append-only execution and receipt facts."""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.cycles.effects.manifest import ActionKind
from apps.cycles.records import ClientRecord


class PublicationState(models.TextChoices):
    """A withdrawal never erases the originally published bytes or receipts."""

    PUBLISHED = "publicada", "Publicada"
    WITHDRAWN = "retirada", "Retirada"


class ClientPublication(ClientRecord):
    """Portal-readable content, with no private proposal or review relation reads."""

    cycle = models.ForeignKey(
        "cycles.ServiceCycle", on_delete=models.PROTECT, related_name="publications"
    )
    proposal = models.ForeignKey(
        "cycles.ActionProposal", on_delete=models.PROTECT, related_name="publications"
    )
    proposal_revision = models.PositiveIntegerField()
    title = models.CharField(max_length=255)
    body_ptbr = models.TextField()
    # Public selected link identities and their exact hash/revision projection.
    documents = models.JSONField(default=list)
    publication_digest = models.CharField(max_length=64)
    business_intent_key = models.CharField(max_length=64)
    state = models.CharField(
        max_length=12,
        choices=PublicationState.choices,
        default=PublicationState.PUBLISHED,
    )
    published_at = models.DateTimeField(default=timezone.now)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    redacted_at = models.DateTimeField(null=True)
    supersedes = models.ForeignKey(
        "self", on_delete=models.PROTECT, related_name="revisions", null=True
    )

    class Meta(ClientRecord.Meta):
        """Every natural and exact-version identity includes the client."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "client", "business_intent_key"],
                name="publication_intent_uniq",
            ),
            models.UniqueConstraint(
                fields=["tenant", "client", "id", "publication_digest"],
                name="publication_digest_uniq",
            ),
            models.UniqueConstraint(
                fields=["tenant", "client", "supersedes"],
                name="publication_single_revision",
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=PublicationState.values, version__gt=0),
                name="publication_valid_state",
            ),
        ]


class ClientAcknowledgement(ClientRecord):
    """An explicit receipt of this publication digest, not an email read receipt."""

    publication = models.ForeignKey(
        ClientPublication, on_delete=models.PROTECT, related_name="acknowledgements"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="publication_acknowledgements",
    )
    acknowledged_at = models.DateTimeField(default=timezone.now)
    publication_digest = models.CharField(max_length=64)

    class Meta(ClientRecord.Meta):
        """A repeated receipt POST converges without updating an old receipt."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=[
                    "tenant",
                    "client",
                    "publication",
                    "user",
                    "publication_digest",
                ],
                name="acknowledgement_person_uniq",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["tenant", "client"], name="clientack_scope_idx"),
        ]


class ActionExecution(ClientRecord):
    """Internal effect/queueing barrier; SMTP outcome remains in the delivery ledger."""

    cycle = models.ForeignKey(
        "cycles.ServiceCycle", on_delete=models.PROTECT, related_name="executions"
    )
    proposal = models.ForeignKey(
        "cycles.ActionProposal", on_delete=models.PROTECT, related_name="executions"
    )
    proposal_revision = models.PositiveIntegerField()
    digest = models.CharField(max_length=64)
    business_intent_key = models.CharField(max_length=64)
    action_kind = models.CharField(max_length=32)
    executed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="executed_actions",
    )
    executed_at = models.DateTimeField(default=timezone.now)

    class Meta(ClientRecord.Meta):
        """A changed digest cannot reuse the same business operation."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "client", "business_intent_key"],
                name="execution_intent_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(action_kind__in=[kind.value for kind in ActionKind]),
                name="execution_known_kind",
            ),
        ]
