"""Replay authorizes the existing case; current eligibility gates only new cases."""

from uuid import UUID

import pytest
from django.core.exceptions import ValidationError
from django.http import Http404

from apps.clients.maintenance import EngagementData, save_engagement
from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleEvent, PlaybookVersion, ServiceCycle
from apps.cycles.rules import StepSpec
from apps.cycles.services import playbooks
from apps.cycles.services.cycle import open_cycle
from tests.cycles.cycle_fixtures import PERIOD, CycleSetup, cycle_setup

pytestmark = pytest.mark.django_db(transaction=True)


def _publish_distinct_version(
    setup: CycleSetup, playbook_id: UUID, *, regimes: list[str]
) -> PlaybookVersion:
    draft = playbooks.create_draft_version(
        actor=setup.actor, tenant=setup.tenant, playbook_pk=playbook_id
    )
    revised = playbooks.update_draft(
        actor=setup.actor,
        tenant=setup.tenant,
        playbook_pk=playbook_id,
        number=draft.number,
        expected_version=draft.version,
        applies_to={"regimes": regimes, "service_kinds": ["mei_mensal"]},
        steps=[
            StepSpec(
                key="new-node",
                kind="task",
                label_ptbr="New node",
                due_rule={"kind": "period_end_offset", "days": 5},
            )
        ],
    )
    published, _ = playbooks.publish_version(
        actor=setup.actor,
        tenant=setup.tenant,
        playbook_pk=playbook_id,
        number=revised.number,
        expected_version=revised.version,
    )
    return published


@pytest.mark.parametrize("change", ["retired", "new_scope", "engagement_scope"])
def test_existing_cycle_replays_after_lifecycle_changes_without_rewriting_graph(
    change: str,
) -> None:
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        clone = playbooks.clone_playbook(
            actor=setup.actor,
            tenant=setup.tenant,
            playbook_pk=setup.definition.playbook_id,
        )
        first, _ = playbooks.publish_version(
            actor=setup.actor,
            tenant=setup.tenant,
            playbook_pk=clone.pk,
            number=1,
            expected_version=1,
        )
        original = open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            first.pk,
            idempotency_key="original-request",
        )
        cycle = ServiceCycle.objects.get(pk=original.id)
        graph = list(cycle.tasks.order_by("step_key").values())
        pinned_profile_id = cycle.tax_profile_id
        second = _publish_distinct_version(setup, clone.pk, regimes=["mei"])
        assert list(second.steps.values_list("key", flat=True)) == ["new-node"]
        assert "new-node" not in {task["step_key"] for task in graph}
        positive = open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            second.pk,
            idempotency_key="different-key",
        )
        assert positive.id == original.id
        assert not positive.created
        assert list(cycle.tasks.order_by("step_key").values()) == graph

        target = first
        if change == "retired":
            playbooks.retire_version(
                actor=setup.actor,
                tenant=setup.tenant,
                playbook_pk=clone.pk,
                number=first.number,
                expected_version=first.version,
            )
        elif change == "new_scope":
            target = _publish_distinct_version(setup, clone.pk, regimes=["simples"])
        else:
            save_engagement(
                actor=setup.actor,
                tenant_id=setup.tenant.pk,
                client_id=setup.client.pk,
                engagement_id=setup.engagement.pk,
                expected_version=setup.engagement.version,
                data=EngagementData(service_kind="documentos_mensal", starts_on=PERIOD),
            )

        event_count = CycleEvent.objects.count()
        replay = open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            target.pk,
            idempotency_key="original-request",
        )
        assert (replay.id, replay.version, replay.state, replay.created) == (
            original.id,
            original.version,
            original.state,
            False,
        )
        cycle.refresh_from_db()
        assert cycle.playbook_version_id == first.pk
        assert cycle.tax_profile_id == pinned_profile_id
        assert list(cycle.tasks.order_by("step_key").values()) == graph
        with pytest.raises(Http404):
            open_cycle(
                setup.staff,
                setup.engagement.pk,
                PERIOD,
                target.pk,
                idempotency_key="unassigned-replay",
            )
        refused = Http404 if change == "retired" else ValidationError
        with pytest.raises(refused):
            open_cycle(
                setup.actor,
                setup.engagement.pk,
                PERIOD.replace(month=10),
                target.pk,
                idempotency_key="new-period",
            )
        assert CycleEvent.objects.count() == event_count
        assert ServiceCycle.objects.count() == 1
