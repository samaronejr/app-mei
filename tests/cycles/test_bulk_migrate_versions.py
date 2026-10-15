"""S15 G: pin migration is per item and never erases work already performed."""

import pytest

from apps.core.tenancy import tenant_context
from apps.cycles.bulk import BulkItemState, BulkKind, BulkOperationState
from apps.cycles.bulk_contracts import Selection
from apps.cycles.models import CycleEvent, CycleState, ServiceCycle, TaskState
from apps.cycles.rules import StepSpec
from apps.cycles.services import playbooks
from apps.cycles.services.bulk import preview_bulk
from apps.cycles.services.cycle import open_cycle, transition_task
from apps.cycles.tasks import run_bulk_operation
from tests.cycles.bulk_fixtures import bulk_setup, confirm, item_states
from tests.cycles.cycle_fixtures import PERIOD

pytestmark = pytest.mark.django_db(transaction=True)


def test_migration_preserves_completed_cycles_and_reports_a_partial_failure() -> None:
    setup = bulk_setup("bulk-migrate")
    graph = setup.graph
    original_step = StepSpec(
        key="review",
        kind="task",
        label_ptbr="Conferir documentos",
        due_rule={"kind": "period_end_offset", "days": 5},
    )
    with tenant_context(graph.tenant.pk):
        playbook = playbooks.create_playbook(
            actor=graph.actor,
            tenant=graph.tenant,
            key="bulk-versioned",
            name="Rotina versionada",
            steps=[original_step],
        )
        playbooks.publish_version(
            actor=graph.actor,
            tenant=graph.tenant,
            playbook_pk=playbook.pk,
            number=1,
            expected_version=1,
        )
        definition = playbook.versions.get(number=1)
        cycles = [
            ServiceCycle.objects.get(
                pk=open_cycle(
                    graph.actor,
                    engagement.pk,
                    PERIOD,
                    definition.pk,
                    idempotency_key=f"fixture:{index}",
                ).id
            )
            for index, engagement in enumerate(setup.engagements)
        ]
        ServiceCycle.objects.filter(pk=cycles[2].pk).update(state=CycleState.COMPLETED)
        draft = playbooks.create_draft_version(
            actor=graph.actor, tenant=graph.tenant, playbook_pk=playbook.pk
        )
        updated = playbooks.update_draft(
            actor=graph.actor,
            tenant=graph.tenant,
            playbook_pk=playbook.pk,
            number=draft.number,
            expected_version=draft.version,
            steps=[
                StepSpec(
                    key="review",
                    kind="review",
                    label_ptbr="Revisão independente",
                    due_rule={"kind": "period_end_offset", "days": 5},
                ),
                StepSpec(
                    key="follow-up",
                    kind="task",
                    label_ptbr="Registrar retorno",
                    depends_on=("review",),
                    due_rule={"kind": "period_end_offset", "days": 6},
                ),
            ],
            applies_to={},
        )
        playbooks.publish_version(
            actor=graph.actor,
            tenant=graph.tenant,
            playbook_pk=playbook.pk,
            number=updated.number,
            expected_version=updated.version,
        )
        target = playbook.versions.get(number=2)
        preview = preview_bulk(
            graph.actor,
            BulkKind.MIGRATE_OPEN_CYCLES,
            Selection(tuple(row.pk for row in cycles), playbook_version_id=target.pk),
        )
        original_ids = {
            row.pk: tuple(row.tasks.values_list("pk", flat=True)) for row in cycles
        }
        assert [row.playbook_version_id for row in cycles] == [definition.pk] * 3
        assert preview.snapshot_json["items"][2]["eligible"] is False
    operation = confirm(graph.actor, preview)

    # This independently versioned node starts after preview. Relabelling it as
    # different work would erase what its operator actually did.
    with tenant_context(graph.tenant.pk):
        task = cycles[1].tasks.get()
        transition_task(
            graph.actor, task.pk, TaskState.IN_PROGRESS, expected_version=task.version
        )
    run_bulk_operation(str(operation.tenant_id), str(operation.pk))
    run_bulk_operation(str(operation.tenant_id), str(operation.pk))

    assert item_states(operation) == {
        cycles[0].pk: BulkItemState.DONE,
        cycles[1].pk: BulkItemState.FAILED,
        cycles[2].pk: BulkItemState.SKIPPED_INELIGIBLE,
    }
    with tenant_context(operation.tenant_id):
        for row in cycles:
            row.refresh_from_db()
        assert [row.playbook_version_id for row in cycles] == [
            target.pk,
            definition.pk,
            definition.pk,
        ]
        assert cycles[2].state == CycleState.COMPLETED
        for row in cycles:
            assert set(original_ids[row.pk]).issubset(
                row.tasks.values_list("pk", flat=True)
            )
        assert cycles[0].tasks.get(step_key="follow-up").state == TaskState.BLOCKED
        assert cycles[1].tasks.count() == 1
        assert cycles[1].tasks.get().state == TaskState.IN_PROGRESS
        assert CycleEvent.objects.filter(type="cycle.playbook_migrated").count() == 1
        operation.refresh_from_db()
        assert operation.state == BulkOperationState.COMPLETED_WITH_FAILURES
