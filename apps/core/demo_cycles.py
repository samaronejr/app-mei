"""Twelve months of real scoped cycles and missing-document requests."""

from calendar import monthrange
from datetime import date

from apps.accounts.models import User
from apps.clients.models import ClientCompany, Engagement, TaxProfile
from apps.core.demo_data import HISTORY_START, ScaleSpec, demo_id
from apps.cycles.models import (
    CycleEvent,
    CycleState,
    CycleTask,
    EvidenceRequest,
    PlaybookVersion,
    RequestInternal,
    RequestState,
    ServiceCycle,
    TaskState,
)
from apps.cycles.packs import install_system_packs
from apps.tenants.models import Tenant


def seed_cycles(tenant: Tenant, actor: User, spec: ScaleSpec) -> list[EvidenceRequest]:
    """Populate history in monthly batches so the growth seed stays memory-bounded."""
    install_system_packs(tenant)
    definitions = {
        version.playbook.key: version
        for version in PlaybookVersion.objects.filter(state="published")
        .select_related("playbook")
        .prefetch_related("steps")
    }
    clients = list(ClientCompany.objects.order_by("cnpj"))
    profiles = {row.client_id: row for row in TaxProfile.objects.all()}
    engagements = {row.client_id: row for row in Engagement.objects.all()}
    requests: list[EvidenceRequest] = []
    for offset in range(12):
        year, month = divmod(
            HISTORY_START.year * 12 + HISTORY_START.month - 1 + offset, 12
        )
        period = date(year, month + 1, 1)
        end = period.replace(day=monthrange(year, month + 1)[1])
        cycles: list[ServiceCycle] = []
        tasks: list[CycleTask] = []
        requests = []
        for index, client in enumerate(clients):
            definition = definitions["mei-mensal" if client.is_mei else "coleta-mensal"]
            cycle = ServiceCycle(
                id=demo_id(spec.seed, f"cycle:{offset}:{index}"),
                tenant=tenant,
                client=client,
                engagement=engagements[client.pk],
                playbook=definition.playbook,
                playbook_version=definition,
                tax_profile=profiles[client.pk],
                period_start=period,
                period_end=end,
                state=CycleState.WAITING,
                owner=actor,
                created_at=spec.now,
            )
            cycles.append(cycle)
            tasks.extend(
                CycleTask(
                    id=demo_id(spec.seed, f"task:{offset}:{index}:{step.key}"),
                    tenant=tenant,
                    client=client,
                    cycle=cycle,
                    step_key=step.key,
                    state=TaskState.READY if not step.depends_on else TaskState.BLOCKED,
                    owner=actor,
                    due_on=end,
                )
                for step in definition.steps.all()
                if step.is_enabled
            )
            requests.append(
                EvidenceRequest(
                    id=demo_id(spec.seed, f"request:{offset}:{index}"),
                    tenant=tenant,
                    client=client,
                    cycle=cycle,
                    title=f"Documentos sintéticos de {period:%m/%Y}",
                    client_note="Envie as notas e os extratos da empresa.",
                    due_on=end,
                    state=RequestState.REQUESTED,
                    requested_at=spec.now,
                    created_at=spec.now,
                    created_by=actor,
                )
            )
        ServiceCycle.objects.bulk_create(cycles)
        CycleTask.objects.bulk_create(tasks, batch_size=1_000)
        EvidenceRequest.objects.bulk_create(requests)
        RequestInternal.objects.bulk_create(
            [
                RequestInternal(
                    id=demo_id(spec.seed, f"internal:{offset}:{index}"),
                    tenant=tenant,
                    client=request.client,
                    request=request,
                    instructions="Dado sintético; conferir a empresa antes de aceitar.",
                )
                for index, request in enumerate(requests)
            ]
        )
        CycleEvent.objects.bulk_create(
            [
                CycleEvent(
                    id=demo_id(spec.seed, f"event:{offset}:{index}"),
                    tenant=tenant,
                    client=cycle.client,
                    cycle=cycle,
                    type="demo.seeded",
                    actor=actor,
                    aggregate_type="servicecycle",
                    aggregate_id=cycle.pk,
                    aggregate_version=1,
                    occurred_at=spec.now,
                    observed_at=spec.now,
                    correlation_id=demo_id(spec.seed, f"correlation:{offset}:{index}"),
                    payload={"seed": spec.seed},
                )
                for index, cycle in enumerate(cycles)
            ]
        )
    return requests
