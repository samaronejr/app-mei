"""Cross-client keys, interrupts and timeline providers exercise real transactions."""

from datetime import date
from uuid import UUID

import pytest
from django.db import IntegrityError, transaction
from django.http import Http404
from django.utils import timezone

from apps.clients.models import ClientCompany, Engagement
from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleEvent, CycleTask, ServiceCycle
from apps.cycles.services import cycle as service
from apps.cycles.workspace import OUTBOX_TIMELINE_READERS, TimelineEntry, timeline
from tests.cycles.cycle_fixtures import PERIOD, cycle_setup

pytestmark = pytest.mark.django_db(transaction=True)


def test_repeated_interruptions_roll_back_the_graph_and_resume_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = cycle_setup()
    instantiate = service._instantiate

    def interrupted(cycle: ServiceCycle) -> None:
        instantiate(cycle)
        raise InterruptedError

    with tenant_context(setup.tenant.pk):
        for _ in range(3):
            with monkeypatch.context() as patch:
                patch.setattr(service, "_instantiate", interrupted)
                with pytest.raises(InterruptedError):
                    service.open_cycle(
                        setup.actor,
                        setup.engagement.pk,
                        PERIOD,
                        setup.definition.pk,
                        idempotency_key="resume",
                    )
            assert not ServiceCycle.objects.exists()
            assert not CycleTask.objects.exists()
            assert not CycleEvent.objects.exists()
        first = service.open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            setup.definition.pk,
            idempotency_key="resume",
        )
        replay = service.open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            setup.definition.pk,
            idempotency_key="resume",
        )
        assert first.created
        assert not replay.created
        assert first.id == replay.id
        assert CycleEvent.objects.count() == 1


def test_cross_client_and_tenant_references_are_rejected() -> None:
    setup = cycle_setup()
    foreign = cycle_setup("foreign-cycle")
    with tenant_context(setup.tenant.pk):
        with pytest.raises(Http404):
            service.open_cycle(
                setup.actor,
                foreign.engagement.pk,
                PERIOD,
                setup.definition.pk,
                idempotency_key="foreign",
            )
        assert not ServiceCycle.objects.exists()
        ref = service.open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            setup.definition.pk,
            idempotency_key="paired",
        )
        other = ClientCompany.objects.create(
            tenant=setup.tenant, legal_name="Sibling", cnpj="11444777000161"
        )
        engagement = Engagement.objects.create(
            tenant=setup.tenant,
            client=other,
            service_kind="mei_mensal",
            starts_on=PERIOD,
            provenance="staff_entry",
        )
        for changes in [
            {"engagement_id": engagement.pk},
            {"client_id": other.pk},
            {"playbook_version_id": foreign.definition.pk},
        ]:
            with pytest.raises(IntegrityError), transaction.atomic():
                ServiceCycle.objects.filter(pk=ref.id).update(**changes)
        task = CycleTask.objects.get(cycle_id=ref.id, step_key="notas-fiscais")
        with pytest.raises(IntegrityError), transaction.atomic():
            CycleTask.objects.filter(pk=task.pk).update(client_id=other.pk)
        assert CycleEvent.objects.count() == 1


def test_outbox_hook_receives_explicit_scope_and_merges_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = cycle_setup()
    entry = TimelineEntry(
        setup.engagement.pk,
        timezone.now(),
        "Outbox reference",
        "Sistema / agenda",
        str(setup.engagement.pk),
    )

    def provider(tenant_id: UUID, client_id: UUID, period: date) -> list[TimelineEntry]:
        assert (tenant_id, client_id, period) == (
            setup.tenant.pk,
            setup.client.pk,
            PERIOD,
        )
        return [entry]

    monkeypatch.setattr(
        "apps.cycles.workspace.OUTBOX_TIMELINE_READERS",
        [*OUTBOX_TIMELINE_READERS, provider],
    )
    with tenant_context(setup.tenant.pk):
        assert timeline(setup.actor, setup.client.pk, PERIOD) == [entry]
