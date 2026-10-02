"""Protected frozen proposals and append-only decisions bound to exact bytes."""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.cycles.effects.manifest import ActionKind
from apps.cycles.records import ClientRecord


class ProposalState(models.TextChoices):
    """Approval is a decision, never evidence that an effect was executed."""

    AWAITING = "aguardando_aprovacao", "Aguardando aprova\u00e7\u00e3o"
    APPROVED = "aprovada", "Aprovada"
    REJECTED = "rejeitada", "Rejeitada"
    INVALIDATED = "invalidada", "Invalidada"
    CANCELLED = "cancelada", "Cancelada"


class ApprovalDecision(models.TextChoices):
    """The immutable answer one eligible person gave to one proposal revision."""

    APPROVE = "approve", "Aprovar"
    REJECT = "reject", "N\u00e3o aprovar"


class ActionProposal(ClientRecord):
    """One immutable effect revision; only its lifecycle and private reason change.

    input_spec contains selected input identities and rendered-content inputs,
    never authority supplied by a request. manifest_bytes is the frozen card and
    eventual execution source. A revised proposal is another row, not an overwrite.
    """

    cycle = models.ForeignKey(
        "cycles.ServiceCycle", on_delete=models.PROTECT, related_name="proposals"
    )
    previous = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="next_revisions",
    )
    revision = models.PositiveIntegerField(default=1)
    business_intent_key = models.CharField(max_length=64)
    action_kind = models.CharField(
        max_length=32,
        choices=[
            (ActionKind.CLIENT_PUBLICATION, "Publicar documentos"),
            (ActionKind.EMAIL_NOTICE, "Enviar e-mail"),
            (ActionKind.CYCLE_COMPLETION, "Concluir ciclo"),
            (ActionKind.REQUEST_WAIVER, "Dispensar solicita\u00e7\u00e3o"),
        ],
    )
    input_spec = models.JSONField()
    manifest_bytes = models.BinaryField()
    digest = models.CharField(max_length=64)
    state = models.CharField(
        max_length=24, choices=ProposalState.choices, default=ProposalState.AWAITING
    )
    initiator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="effect_proposals",
    )
    not_before = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    version = models.PositiveIntegerField(default=1)
    invalidation_reasons = models.JSONField(default=list)
    # User prose is erasable protected content, not an append-only audit payload.
    decision_reason = models.TextField(blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(ClientRecord.Meta):
        """Scoped intent lineage and an exact referent for approval decisions."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "client", "business_intent_key", "revision"],
                name="actionproposal_intent_revision_uniq",
            ),
            models.UniqueConstraint(
                fields=["tenant", "client", "id", "revision", "digest"],
                name="actionproposal_manifest_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gt=0, version__gt=0),
                name="actionproposal_positive_revisions",
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=ProposalState.values),
                name="actionproposal_known_state",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            *ClientRecord.Meta.indexes,
            models.Index(
                fields=["tenant", "state", "created_at"],
                name="actionproposal_queue_idx",
            ),
        ]


class Approval(ClientRecord):
    """A decision record: identifiers, hashes, codes and authority timestamps only."""

    proposal = models.ForeignKey(
        ActionProposal, on_delete=models.PROTECT, related_name="approvals"
    )
    proposal_revision = models.PositiveIntegerField()
    digest = models.CharField(max_length=64)
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="effect_approvals",
    )
    # Historical authority identity must survive a membership's later deletion.
    membership_id = models.UUIDField()
    membership_version = models.PositiveIntegerField()
    authorization_snapshot = models.JSONField()
    decision = models.CharField(max_length=8, choices=ApprovalDecision.choices)
    decided_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()

    class Meta(ClientRecord.Meta):
        """One answer per person per exact proposal revision, always client-scoped."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=[
                    "tenant",
                    "client",
                    "proposal",
                    "proposal_revision",
                    "approver",
                ],
                name="approval_person_revision_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(decision__in=ApprovalDecision.values),
                name="approval_known_decision",
            ),
            models.CheckConstraint(
                condition=models.Q(proposal_revision__gt=0, membership_version__gt=0),
                name="approval_positive_revisions",
            ),
            models.CheckConstraint(
                condition=models.Q(decision=ApprovalDecision.REJECT)
                | models.Q(expires_at__gt=models.F("decided_at")),
                name="approval_decision_before_expiry",
            ),
        ]
