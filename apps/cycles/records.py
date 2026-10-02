"""Client-period aggregates, private work and the minimized append-only envelope."""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.identifiers import uuid7
from apps.core.models import TenantScopedModel


class ClientRecord(TenantScopedModel):
    """Every child carries both isolation dimensions and a paired-FK referent."""

    client = models.ForeignKey(
        "clients.ClientCompany", on_delete=models.PROTECT, related_name="+"
    )

    class Meta(TenantScopedModel.Meta):
        """Shared client-paired identity and tenant-led index."""

        abstract = True
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tenant", "client", "id"], name="%(class)s_scope_id_uniq"
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["tenant", "client"], name="%(class)s_scope_idx"),
        ]


class CycleState(models.TextChoices):
    """Work states, never payment, filing or delivery assertions."""

    PLANNED = "planejado", "Planejado"
    ACTIVE = "em_andamento", "Em andamento"
    WAITING = "aguardando_cliente", "Aguardando cliente"
    REVIEW = "em_revisao", "Em revisão"
    APPROVAL = "aguardando_aprovacao", "Aguardando aprovação"
    EXECUTION = "em_execucao", "Em execução"
    COMPLETED = "concluido", "Concluído"
    BLOCKED = "bloqueado", "Bloqueado"
    WITH_PENDING = "encerrado_com_pendencias", "Encerrado com pendências"
    CANCELLED = "cancelado", "Cancelado"


class ServiceCycle(ClientRecord):
    """One immutable definition pinned to a client's local competence month.

    This table contains no internal prose: the portal may SELECT it under client
    RLS. Reasons, notes and completion snapshots live in private child tables.
    """

    engagement = models.ForeignKey(
        "clients.Engagement", on_delete=models.PROTECT, related_name="cycles"
    )
    playbook = models.ForeignKey(
        "cycles.Playbook", on_delete=models.PROTECT, related_name="cycles"
    )
    playbook_version = models.ForeignKey(
        "cycles.PlaybookVersion", on_delete=models.PROTECT, related_name="cycles"
    )
    tax_profile = models.ForeignKey(
        "clients.TaxProfile", on_delete=models.PROTECT, null=True, related_name="cycles"
    )
    period_start = models.DateField()
    period_end = models.DateField()
    state = models.CharField(
        max_length=32, choices=CycleState.choices, default=CycleState.PLANNED
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        related_name="owned_cycles",
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reviewed_cycles",
    )
    version = models.PositiveIntegerField(default=1)
    amendment = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(ClientRecord.Meta):
        """Natural generation key and local-month validity."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "client", "engagement", "playbook", "period_start"],
                name="servicecycle_period_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(period_start__day=1),
                name="servicecycle_first_of_month",
            ),
            models.CheckConstraint(
                condition=models.Q(period_end__gte=models.F("period_start")),
                name="servicecycle_period_order",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            *ClientRecord.Meta.indexes,
            models.Index(
                fields=["tenant", "period_start", "state"],
                name="cycle_period_state_idx",
            ),
        ]


class TaskState(models.TextChoices):
    """A dependency graph node's work state."""

    BLOCKED = "blocked", "Bloqueada"
    READY = "ready", "Pronta"
    IN_PROGRESS = "in_progress", "Em andamento"
    SUBMITTED = "submitted", "Em revisão"
    ACCEPTED = "accepted", "Aceita"
    WAIVED = "waived", "Dispensada"
    CANCELLED = "cancelled", "Cancelada"


class CycleTask(ClientRecord):
    """Private work node; labels and dependencies come from the pinned step."""

    cycle = models.ForeignKey(
        ServiceCycle, on_delete=models.PROTECT, related_name="tasks"
    )
    step_key = models.SlugField(max_length=64)
    state = models.CharField(
        max_length=20, choices=TaskState.choices, default=TaskState.BLOCKED
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        related_name="cycle_tasks",
    )
    due_on = models.DateField(null=True)
    statutory_due_on = models.DateField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta(ClientRecord.Meta):
        """One task per enabled step of the pinned definition."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "client", "cycle", "step_key"],
                name="cycletask_step_uniq",
            ),
        ]


class CycleAmendment(ClientRecord):
    """A reopening preserves the completed snapshot rather than rewriting it."""

    cycle = models.ForeignKey(
        ServiceCycle, on_delete=models.PROTECT, related_name="amendments"
    )
    number = models.PositiveIntegerField()
    reason = models.TextField()
    pinned_playbook_version = models.ForeignKey(
        "cycles.PlaybookVersion", on_delete=models.PROTECT, related_name="amendments"
    )
    affected_scope = models.JSONField(default=list)
    completed_snapshot = models.JSONField(default=dict)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="cycle_amendments",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(ClientRecord.Meta):
        """Amendment sequence within one client cycle."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "client", "cycle", "number"],
                name="cycleamendment_number_uniq",
            ),
        ]


class CycleEvent(ClientRecord):
    """S7.7 envelope; payloads contain only identifiers, codes and hashes.

    Database grants AND the shared append-only trigger forbid mutation. Internal
    prose belongs in InternalNote; event producers cannot accept arbitrary JSON.
    """

    cycle = models.ForeignKey(
        ServiceCycle, on_delete=models.PROTECT, related_name="events"
    )
    type = models.CharField(max_length=64)
    schema_version = models.PositiveIntegerField(default=1)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        related_name="cycle_events",
    )
    aggregate_type = models.CharField(max_length=32)
    aggregate_id = models.UUIDField()
    aggregate_version = models.PositiveIntegerField()
    occurred_at = models.DateTimeField(default=timezone.now)
    observed_at = models.DateTimeField(default=timezone.now)
    correlation_id = models.UUIDField(default=uuid7)
    causation_id = models.UUIDField(null=True)
    payload = models.JSONField(default=dict)


class InternalNote(ClientRecord):
    """Firm-only editable text, never copied into append-only payloads."""

    cycle = models.ForeignKey(
        ServiceCycle, on_delete=models.PROTECT, related_name="notes"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="cycle_notes"
    )
    body = models.TextField()
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)


class ExceptionState(models.TextChoices):
    """Exception resolution is separate from cycle completion."""

    OPEN = "aberta", "Aberta"
    HANDLING = "em_tratamento", "Em tratamento"
    RESOLVED = "resolvida", "Resolvida"
    RISK_ACCEPTED = "aceita_como_risco", "Aceita como risco"
    CANCELLED = "cancelada", "Cancelada"


class Exception(ClientRecord):  # noqa: A001 - S6.3's domain model name
    """An owned problem with an explicit next action and review/deadline.

    Portal SELECT since T14, confined by RLS to the client AND `client_visible` rows.
    Instructions and evidence references live in ExceptionInternal; resolution
    prose lives in InternalNote. Neither private table is granted to the portal.
    """

    cycle = models.ForeignKey(
        ServiceCycle, on_delete=models.PROTECT, related_name="exceptions"
    )
    subject_ref = models.UUIDField()
    reason_code = models.SlugField(max_length=64)
    state = models.CharField(
        max_length=24, choices=ExceptionState.choices, default=ExceptionState.OPEN
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="cycle_exceptions",
    )
    due_on = models.DateField(null=True, blank=True)
    review_on = models.DateField(null=True, blank=True)
    blocks_completion = models.BooleanField(default=True)
    client_visible = models.BooleanField(default=False)
    client_message = models.CharField(max_length=500, blank=True, default="")
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(ClientRecord.Meta):
        """Converge active problems and require a review or due date."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "client", "cycle", "reason_code", "subject_ref"],
                condition=models.Q(state__in=["aberta", "em_tratamento"]),
                name="exception_open_subject_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(due_on__isnull=False)
                | models.Q(review_on__isnull=False),
                name="exception_has_review_date",
            ),
            models.CheckConstraint(
                condition=models.Q(client_visible=False) | ~models.Q(client_message=""),
                name="exception_visible_has_message",
            ),
        ]


class ExceptionInternal(ClientRecord):
    """Firm-only instructions and evidence, owned by the exception's version."""

    exception = models.ForeignKey(
        Exception, on_delete=models.PROTECT, related_name="internals"
    )
    next_action = models.CharField(max_length=255)
    evidence_refs = models.JSONField(default=list)

    class Meta(ClientRecord.Meta):
        """One private companion per scoped exception, like RequestInternal."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "client", "exception"],
                name="exceptioninternal_exception_uniq",
            ),
        ]
