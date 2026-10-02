"""Locked, versioned writes with read-only replays and unique-key creation."""

import calendar
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from psycopg.errors import UniqueViolation

from apps.accounts.models import User
from apps.authz.services import can
from apps.clients.maintenance import load_client
from apps.clients.models import (
    AssignmentRole,
    ClientAssignment,
    ClientCompany,
    ClientStatus,
    Engagement,
    TaxRegime,
)
from apps.clients.services import profile_at
from apps.core.flags import tenant_has
from apps.core.tenancy import MissingTenantContext, current_tenant_id
from apps.cycles.capabilities import CYCLES_COMPLETE, CYCLES_OPERATE, CYCLES_REVIEW
from apps.cycles.errors import IllegalTransition, VersionConflict
from apps.cycles.models import (
    CycleEvent,
    CycleState,
    CycleTask,
    InternalNote,
    OwnerRule,
    PlaybookStep,
    PlaybookVersion,
    ServiceCycle,
    TaskState,
    VersionState,
)
from apps.cycles.models import Exception as CycleException
from apps.cycles.targeting import AppliesTo
from apps.obligations.models import Obligation

OPEN_STATES: Final = frozenset(CycleState.values) - {
    CycleState.COMPLETED,
    CycleState.WITH_PENDING,
    CycleState.CANCELLED,
}
NORMAL_TRANSITIONS: Final[dict[str, set[str]]] = {
    CycleState.PLANNED: {CycleState.ACTIVE},
    CycleState.ACTIVE: {CycleState.WAITING, CycleState.REVIEW},
    CycleState.WAITING: {CycleState.ACTIVE},
    CycleState.REVIEW: {CycleState.ACTIVE, CycleState.APPROVAL},
    CycleState.APPROVAL: {CycleState.REVIEW, CycleState.EXECUTION},
    CycleState.EXECUTION: {CycleState.COMPLETED},
    CycleState.BLOCKED: {CycleState.ACTIVE},
}
TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    state: frozenset(NORMAL_TRANSITIONS.get(state, set()))
    | (
        frozenset({CycleState.BLOCKED, CycleState.WITH_PENDING, CycleState.CANCELLED})
        - {state}
        if state in OPEN_STATES
        else frozenset()
    )
    for state in CycleState.values
}
# T11/T15/T18 register guards from their own domains. Completion fails closed
# until the evidence-index producer exists; an absent integration is not evidence.
COMPLETION_GUARDS: dict[str, Callable[[ServiceCycle], bool]] = {}
EFFECT_GUARDS: dict[str, Callable[[ServiceCycle], bool]] = {}


@dataclass(frozen=True, slots=True)
class CycleRef:
    """The result distinguishes insertion from replay; the unique key settles races."""

    id: UUID
    version: int
    state: str
    created: bool = False


def tenant_id() -> UUID:
    """Require the context that middleware and TenantTask establish."""
    value = current_tenant_id.get()
    if value is None:
        msg = "A cycle operation requires a tenant."
        raise MissingTenantContext(msg)
    return value


def require_actor(actor: User, capability: str) -> None:
    """Re-evaluate authorization inside the aggregate lock."""
    if not can(actor, capability, tenant_id=tenant_id()):
        raise PermissionDenied


def require_enabled() -> None:
    """Unflagged tenants never create cycle records, including direct services."""
    if not tenant_has(tenant_id(), "cycles_r1"):
        raise Http404


def check_version(actual: int, expected: int) -> None:
    """Refuse stale callers before either state or event is written."""
    if actual != expected:
        msg = "Este registro mudou. Recarregue antes de salvar."
        raise VersionConflict(msg)


def load_cycle(actor: User, cycle_id: UUID, *, lock: bool = False) -> ServiceCycle:
    """Repeat portfolio scope before any write; an id is never authority."""
    require_enabled()
    cycle = ServiceCycle.objects.filter(pk=cycle_id).first()
    if cycle is None:
        raise Http404
    load_client(actor, tenant_id(), cycle.client_id)
    if lock:
        cycle = ServiceCycle.objects.select_for_update().get(pk=cycle.pk)
        load_client(actor, tenant_id(), cycle.client_id)
    return cycle


def emit(
    cycle: ServiceCycle,
    actor: User | None,
    event_type: str,
    *,
    aggregate: CycleTask | InternalNote | CycleException | None = None,
    payload: dict[str, str | int] | None = None,
) -> CycleEvent:
    """Produce an internal event from known ids/codes, never user text."""
    subject = aggregate or cycle
    return CycleEvent.objects.create(
        tenant_id=cycle.tenant_id,
        client_id=cycle.client_id,
        cycle=cycle,
        actor=actor,
        type=event_type,
        aggregate_type=type(subject).__name__,
        aggregate_id=subject.pk,
        aggregate_version=subject.version,
        payload=payload or {},
    )


def _period_end(period: date) -> date:
    if period.day != 1:
        msg = "A competência deve começar no primeiro dia do mês."
        raise ValidationError(msg)
    return period.replace(day=calendar.monthrange(period.year, period.month)[1])


def _due(step: PlaybookStep, cycle: ServiceCycle) -> tuple[date | None, date | None]:
    rule = step.due_rule_json
    if rule["kind"] == "period_end_offset":
        return cycle.period_end + timedelta(days=rule["days"]), None
    if rule["kind"] == "fixed_day":
        month = cycle.period_start.month - 1 + rule["month_offset"]
        return date(
            cycle.period_start.year + month // 12, month % 12 + 1, rule["day"]
        ), None
    statutory = (
        Obligation.objects.filter(
            client_id=cycle.client_id,
            competence_month=cycle.period_start,
            obligation_type_id=rule["obligation"],
        )
        .values_list("resolved_due_date", flat=True)
        .first()
    )
    # No calendar row means unknown, not a fabricated statutory deadline.
    return statutory, statutory


def _instantiate(cycle: ServiceCycle) -> None:
    steps = list(cycle.playbook_version.steps.filter(is_enabled=True))
    enabled = {step.key for step in steps}
    primary = (
        ClientAssignment.objects.filter(
            client_id=cycle.client_id, role=AssignmentRole.PRIMARY
        )
        .values_list("user_id", flat=True)
        .first()
    )
    for step in steps:
        due, statutory = _due(step, cycle)
        owner_id = (
            cycle.owner_id
            if step.owner_rule == OwnerRule.ENGAGEMENT_RESPONSIBLE
            else primary
            if step.owner_rule == OwnerRule.PRIMARY_ASSIGNEE
            else None
        )
        CycleTask.objects.create(
            tenant_id=cycle.tenant_id,
            client_id=cycle.client_id,
            cycle=cycle,
            step_key=step.key,
            state=TaskState.BLOCKED
            if enabled.intersection(step.depends_on)
            else TaskState.READY,
            owner_id=owner_id,
            due_on=due,
            statutory_due_on=statutory,
        )


def _engagement(actor: User | None, engagement_id: UUID) -> Engagement:
    engagement = Engagement.objects.filter(pk=engagement_id).first()
    if engagement is None:
        raise Http404
    if actor is None:
        # The trusted scheduler has no user. The same client lock and eligibility
        # checks apply; it cannot request transitions or mutate existing cycles.
        ClientCompany.objects.select_for_update().get(pk=engagement.client_id)
    else:
        load_client(actor, tenant_id(), engagement.client_id, lock=True)
        require_actor(actor, CYCLES_OPERATE)
    return (
        Engagement.objects.select_for_update()
        .select_related("client")
        .get(pk=engagement.pk)
    )


def open_cycle(
    actor: User | None,
    engagement_id: UUID,
    period: date,
    playbook_version_id: UUID,
    *,
    idempotency_key: str,
) -> CycleRef:
    """Return an authorized existing case, or create an eligible new one.

    Replay is read-only and never re-runs creation eligibility. On a miss, current
    eligibility applies, then the natural-key constraint arbitrates concurrent
    inserts. The caller key is hashed for correlation, not used as a second key.
    """
    require_enabled()
    with transaction.atomic():
        engagement = _engagement(actor, engagement_id)
        definition = PlaybookVersion.objects.filter(pk=playbook_version_id).first()
        if definition is None:
            raise Http404
        existing_cycles = ServiceCycle.objects.select_for_update().filter(
            client_id=engagement.client_id,
            engagement=engagement,
            playbook_id=definition.playbook_id,
            period_start=period,
        )
        existing = existing_cycles.first()
        if existing is not None:
            return CycleRef(existing.pk, existing.version, existing.state)

        # Only a genuinely new case consults today's creation eligibility.
        end = _period_end(period)
        if definition.state != VersionState.PUBLISHED:
            raise Http404
        profile = profile_at(engagement.client, period)
        scope = AppliesTo.parse(definition.applies_to_json)
        regime = profile.regime if profile else TaxRegime.UNKNOWN
        if (
            engagement.client.status != ClientStatus.ACTIVE
            or engagement.starts_on > period
            or (engagement.ends_on is not None and engagement.ends_on <= period)
            or (
                scope.service_kinds
                and engagement.service_kind not in scope.service_kinds
            )
            or (scope.regimes and regime not in scope.regimes)
        ):
            msg = (
                "O serviço e o perfil fiscal não correspondem "
                "a esta competência e playbook."
            )
            raise ValidationError(msg)
        candidate = ServiceCycle(
            tenant_id=tenant_id(),
            client_id=engagement.client_id,
            engagement=engagement,
            playbook_id=definition.playbook_id,
            playbook_version=definition,
            tax_profile=profile,
            period_start=period,
            period_end=end,
            owner_id=engagement.responsible_id,
        )
        try:
            # A missed read does not prove uniqueness. Roll back only this INSERT
            # if another writer wins, leaving the transaction usable to re-read.
            with transaction.atomic():
                candidate.save(force_insert=True)
        except IntegrityError as error:
            if (
                not isinstance(error.__cause__, UniqueViolation)
                or error.__cause__.diag.constraint_name != "servicecycle_period_uniq"
            ):
                raise
            existing = existing_cycles.get()
            return CycleRef(existing.pk, existing.version, existing.state)
        _instantiate(candidate)
        emit(
            candidate,
            actor,
            "cycle.opened",
            payload={
                "playbook_version_id": str(definition.pk),
                "idempotency_hash": hashlib.sha256(
                    idempotency_key.encode()
                ).hexdigest(),
            },
        )
        return CycleRef(candidate.pk, candidate.version, candidate.state, created=True)


def completion_unmet(cycle: ServiceCycle) -> list[str]:
    """Completion is a conjunction, including integrations not installed yet."""
    unmet = []
    if cycle.tasks.exclude(state__in=[TaskState.ACCEPTED, TaskState.WAIVED]).exists():
        unmet.append("tasks")
    if cycle.exceptions.filter(
        blocks_completion=True, state__in=["aberta", "em_tratamento"]
    ).exists():
        unmet.append("exceptions")
    if "evidence_index" not in COMPLETION_GUARDS:
        unmet.append("evidence_index")
    unmet.extend(key for key, check in COMPLETION_GUARDS.items() if not check(cycle))
    return unmet


def transition_cycle(
    actor: User,
    cycle_id: UUID,
    to_state: str,
    *,
    expected_version: int,
    reason: str | None = None,
) -> CycleRef:
    """Apply the guard table under a row lock; terminal snapshots never reopen here."""
    with transaction.atomic():
        cycle = load_cycle(actor, cycle_id, lock=True)
        check_version(cycle.version, expected_version)
        capability = (
            CYCLES_COMPLETE
            if to_state in {CycleState.COMPLETED, CycleState.WITH_PENDING}
            else CYCLES_REVIEW
            if to_state in {CycleState.REVIEW, CycleState.APPROVAL}
            else CYCLES_OPERATE
        )
        require_actor(actor, capability)
        if to_state not in TRANSITIONS[cycle.state]:
            msg = "Esta transição não é permitida."
            raise IllegalTransition(msg)
        if to_state in {CycleState.BLOCKED, CycleState.WITH_PENDING} and not (
            reason and reason.strip()
        ):
            msg = "Informe o motivo antes de continuar."
            raise IllegalTransition(msg)
        if to_state == CycleState.COMPLETED and completion_unmet(cycle):
            msg = "Há condições de conclusão pendentes."
            raise IllegalTransition(msg)
        if to_state == CycleState.CANCELLED and any(
            check(cycle) for check in EFFECT_GUARDS.values()
        ):
            msg = "Um ciclo com efeitos executados não pode ser cancelado."
            raise IllegalTransition(msg)
        previous = cycle.state
        cycle.state = to_state
        cycle.version += 1
        cycle.save(update_fields=["state", "version"])
        if reason and reason.strip():
            InternalNote.objects.create(
                tenant_id=cycle.tenant_id,
                client_id=cycle.client_id,
                cycle=cycle,
                author=actor,
                body=reason.strip(),
            )
        emit(
            cycle,
            actor,
            "cycle.transitioned",
            payload={"from": previous, "to": to_state},
        )
        return CycleRef(cycle.pk, cycle.version, cycle.state)


TASK_TRANSITIONS: Final[dict[str, set[str]]] = {
    TaskState.READY: {TaskState.IN_PROGRESS, TaskState.WAIVED, TaskState.CANCELLED},
    TaskState.BLOCKED: {TaskState.WAIVED, TaskState.CANCELLED},
    TaskState.IN_PROGRESS: {TaskState.SUBMITTED, TaskState.WAIVED, TaskState.CANCELLED},
    TaskState.SUBMITTED: {TaskState.ACCEPTED, TaskState.IN_PROGRESS},
}


def transition_task(
    actor: User,
    task_id: UUID,
    to_state: str,
    *,
    expected_version: int,
    reason: str = "",
) -> CycleTask:
    """Advance one node, then release dependencies from the same pinned graph."""
    with transaction.atomic():
        task = CycleTask.objects.filter(pk=task_id).first()
        if task is None:
            raise Http404
        cycle = load_cycle(actor, task.cycle_id, lock=True)
        task = CycleTask.objects.select_for_update().get(pk=task.pk)
        check_version(task.version, expected_version)
        require_actor(
            actor,
            CYCLES_REVIEW
            if to_state in {TaskState.ACCEPTED, TaskState.WAIVED}
            else CYCLES_OPERATE,
        )
        if cycle.state not in OPEN_STATES or to_state not in TASK_TRANSITIONS.get(
            task.state, set()
        ):
            msg = "Esta tarefa não pode fazer esta transição."
            raise IllegalTransition(msg)
        if to_state == TaskState.WAIVED and not reason.strip():
            msg = "Dispensar uma tarefa exige um motivo."
            raise IllegalTransition(msg)
        task.state = to_state
        task.version += 1
        task.save(update_fields=["state", "version"])
        if reason.strip():
            InternalNote.objects.create(
                tenant_id=cycle.tenant_id,
                client_id=cycle.client_id,
                cycle=cycle,
                author=actor,
                body=reason.strip(),
            )
        emit(
            cycle, actor, "task.transitioned", aggregate=task, payload={"to": to_state}
        )
        tasks = {row.step_key: row for row in cycle.tasks.all()}
        for step in cycle.playbook_version.steps.filter(is_enabled=True):
            node = tasks[step.key]
            dependencies = [tasks[key] for key in step.depends_on if key in tasks]
            if node.state == TaskState.BLOCKED and all(
                dep.state in {TaskState.ACCEPTED, TaskState.WAIVED}
                for dep in dependencies
            ):
                node.state = TaskState.READY
                node.version += 1
                node.save(update_fields=["state", "version"])
                emit(cycle, actor, "task.ready", aggregate=node)
        return task


NOTE_MAX_LENGTH: Final = 10000


def add_note(
    actor: User, cycle_id: UUID, body: str, *, expected_version: int
) -> InternalNote:
    """Private prose uses the cycle revision rendered on the form."""
    if not body.strip() or len(body) > NOTE_MAX_LENGTH:
        msg = "Informe uma nota de até 10.000 caracteres."
        raise ValidationError(msg)
    with transaction.atomic():
        cycle = load_cycle(actor, cycle_id, lock=True)
        check_version(cycle.version, expected_version)
        require_actor(actor, CYCLES_OPERATE)
        note = InternalNote.objects.create(
            tenant_id=cycle.tenant_id,
            client_id=cycle.client_id,
            cycle=cycle,
            author=actor,
            body=body.strip(),
        )
        cycle.version += 1
        cycle.save(update_fields=["version"])
        emit(cycle, actor, "note.added", aggregate=note)
        return note
