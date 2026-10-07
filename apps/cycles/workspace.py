"""Read models for the client-period workspace; no business state is written here."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import Event
from apps.authz.services import can
from apps.clients.maintenance import load_client
from apps.clients.models import TaxProfile
from apps.clients.services import profile_at
from apps.cycles.capabilities import CYCLES_COMPLETE, CYCLES_OPERATE, CYCLES_REVIEW
from apps.cycles.models import CycleEvent, CycleState, ServiceCycle
from apps.cycles.rules import next_period_start
from apps.cycles.services.cycle import (
    TRANSITIONS,
    completion_unmet,
    require_enabled,
    tenant_id,
)


@dataclass(frozen=True, slots=True)
class CycleContext:
    """The S7.1 firm projection; no source data is invented when T6 is absent."""

    tenant_id: UUID
    client_id: UUID
    engagement_id: UUID | None
    period_start: date
    cycle_id: UUID | None
    cycle_state: str | None
    version: int | None
    playbook_version: tuple[UUID, int] | None
    tax_profile: TaxProfile | None
    coverage: tuple[str, ...]
    available_actions: tuple[str, ...]
    visibility: str = "firm"


MONTH_INPUT_LENGTH = 7


def parse_period(value: str | None) -> date:
    """Parse YYYY-MM; do not silently normalize arbitrary dates."""
    if value is None or value == "":
        return timezone.localdate().replace(day=1)
    if len(value) != MONTH_INPUT_LENGTH:
        msg = "Use uma competência no formato AAAA-MM."
        raise ValueError(msg)
    return date.fromisoformat(f"{value}-01")


def available_actions(
    actor: User, cycle: ServiceCycle, *, unmet: list[str] | None = None
) -> tuple[str, ...]:
    """UI only; transition_cycle repeats each decision under the lock."""
    gates = {CYCLES_OPERATE, CYCLES_REVIEW, CYCLES_COMPLETE}
    allowed = {gate: can(actor, gate, tenant_id=cycle.tenant_id) for gate in gates}
    result = []
    for target in sorted(TRANSITIONS[cycle.state]):
        gate = (
            CYCLES_COMPLETE
            if target in {CycleState.COMPLETED, CycleState.WITH_PENDING}
            else CYCLES_REVIEW
            if target in {CycleState.REVIEW, CycleState.APPROVAL}
            else CYCLES_OPERATE
        )
        if allowed[gate] and (
            target != CycleState.COMPLETED
            or not (completion_unmet(cycle) if unmet is None else unmet)
        ):
            result.append(target)
    return tuple(result)


def get_cycle_context(actor: User, client_id: UUID, period: date) -> CycleContext:
    """Resolve the first engagement's context; the workspace lists all cycles."""
    require_enabled()
    client = load_client(actor, tenant_id(), client_id)
    cycle = (
        ServiceCycle.objects.filter(client=client, period_start=period)
        .select_related("playbook_version")
        .order_by("engagement_id", "pk")
        .first()
    )
    return CycleContext(
        tenant_id=tenant_id(),
        client_id=client.pk,
        engagement_id=cycle.engagement_id if cycle else None,
        period_start=period,
        cycle_id=cycle.pk if cycle else None,
        cycle_state=cycle.state if cycle else None,
        version=cycle.version if cycle else None,
        playbook_version=(cycle.playbook_version_id, cycle.playbook_version.number)
        if cycle
        else None,
        tax_profile=profile_at(client, period),
        coverage=("unknown",),
        available_actions=available_actions(actor, cycle) if cycle else (),
    )


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """Reference-only projection; never expose audit metadata or message bodies."""

    id: UUID
    when: datetime
    summary: str
    lane: str
    subject_ref: str


# T10 registers an outbox reference reader. Empty today because no outbox domain
# exists on this base, not because delivery history is claimed to be complete.
OUTBOX_TIMELINE_READERS: list[Callable[[UUID, UUID, date], list[TimelineEntry]]] = []


EVENT_LABELS = {
    "cycle.opened": "Ciclo aberto",
    "cycle.transitioned": "Estado do ciclo alterado",
    "task.transitioned": "Estado de tarefa alterado",
    "task.ready": "Dependências atendidas: tarefa liberada",
    "note.added": "Nota interna adicionada",
    "exception.opened": "Exceção aberta",
}


def timeline(actor: User, client_id: UUID, period: date) -> list[TimelineEntry]:
    """Merge scoped sources, with explicit source lanes and stable ordering."""
    load_client(actor, tenant_id(), client_id)
    events = CycleEvent.objects.filter(
        client_id=client_id, cycle__period_start=period
    ).order_by("-occurred_at", "-pk")[:100]
    entries = [
        TimelineEntry(
            row.pk,
            row.occurred_at,
            EVENT_LABELS.get(row.type, "Evento do ciclo"),
            "Ação da equipe" if row.actor_id else "Sistema / agenda",
            str(row.aggregate_id),
        )
        for row in events
    ]
    next_month = next_period_start(period)
    audits = Event.objects.filter(
        Q(metadata__client_id=str(client_id))
        | Q(
            object_type__in=["ClientCompany", "clients.ClientCompany"],
            object_id=str(client_id),
        ),
        created_at__date__gte=period,
        created_at__date__lt=next_month,
    ).order_by("-created_at", "-pk")[:100]
    entries.extend(
        TimelineEntry(
            row.pk,
            row.created_at,
            "Registro de auditoria",
            "Auditoria / equipe",
            row.object_id,
        )
        for row in audits
    )
    for reader in OUTBOX_TIMELINE_READERS:
        entries.extend(reader(tenant_id(), client_id, period))
    return sorted(entries, key=lambda entry: (entry.when, entry.id), reverse=True)[:100]


def portal_projection(cycle: ServiceCycle) -> dict[str, object]:
    """Exactly three visible fields; never hand a model with private joins to HTML."""
    return {
        "period": cycle.period_start,
        "state_label": cycle.get_state_display(),
        "next_action": "Entre em contato com o escritório para saber o que falta."
        if cycle.state == CycleState.WAITING
        else "Aguarde a próxima orientação do escritório.",
    }
