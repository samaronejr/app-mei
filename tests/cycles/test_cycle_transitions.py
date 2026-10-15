"""The complete transition matrix, stale callers, graph guards and scope."""

from itertools import product
from typing import TYPE_CHECKING

import pytest
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404

from apps.core.tenancy import tenant_context
from apps.cycles.errors import IllegalTransition, VersionConflict
from apps.cycles.models import (
    CycleEvent,
    CycleState,
    CycleTask,
    InternalNote,
    ServiceCycle,
    TaskState,
)
from apps.cycles.services.cycle import (
    COMPLETION_GUARDS,
    EFFECT_GUARDS,
    add_note,
    completion_unmet,
    open_cycle,
    transition_cycle,
    transition_task,
)
from tests.cycles.cycle_fixtures import PERIOD, CycleSetup, cycle_setup

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.django_db

# Independent oracle, not the implementation's TRANSITIONS dict.
NORMAL = {
    "planejado": {"em_andamento"},
    "em_andamento": {"aguardando_cliente", "em_revisao"},
    "aguardando_cliente": {"em_andamento"},
    "em_revisao": {"em_andamento", "aguardando_aprovacao"},
    "aguardando_aprovacao": {"em_revisao", "em_execucao"},
    # Consequential terminal targets use the approved executor since T18.
    # This table covers direct work transitions; completion has its real-flow tests.
    "em_execucao": set(),
    "bloqueado": {"em_andamento"},
}
TERMINAL = {"concluido", "encerrado_com_pendencias", "cancelado"}


@pytest.fixture
def setup() -> CycleSetup:
    return cycle_setup()


def create_cycle(setup: CycleSetup) -> ServiceCycle:
    ref = open_cycle(
        setup.actor,
        setup.engagement.pk,
        PERIOD,
        setup.definition.pk,
        idempotency_key="test",
    )
    return ServiceCycle.objects.get(pk=ref.id)


@pytest.mark.parametrize(
    ("state", "target"), list(product(CycleState.values, repeat=2))
)
def test_cycle_transitions(
    setup: CycleSetup, state: str, target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with tenant_context(setup.tenant.pk):
        cycle = create_cycle(setup)
        # Seed the starting state and satisfied prerequisites; this is not a
        # service bypass in production. The guard itself has negative cases below.
        ServiceCycle.objects.filter(pk=cycle.pk).update(state=state)
        CycleTask.objects.filter(cycle=cycle).update(state=TaskState.ACCEPTED)
        monkeypatch.setitem(COMPLETION_GUARDS, "evidence_index", lambda _: True)
        allowed = NORMAL.get(state, set()) | (
            {"bloqueado", "cancelado"} - {state} if state not in TERMINAL else set()
        )
        before = CycleEvent.objects.count()
        if target in allowed:
            ref = transition_cycle(
                setup.actor,
                cycle.pk,
                target,
                expected_version=1,
                reason="Private reason",
            )
            assert (ref.state, ref.version) == (target, 2)
            assert CycleEvent.objects.count() == before + 1
        else:
            with pytest.raises(IllegalTransition):
                transition_cycle(
                    setup.actor,
                    cycle.pk,
                    target,
                    expected_version=1,
                    reason="Private reason",
                )
            cycle.refresh_from_db()
            assert (cycle.state, cycle.version) == (state, 1)
            assert CycleEvent.objects.count() == before


@pytest.mark.parametrize("target", [CycleState.BLOCKED, CycleState.WITH_PENDING])
def test_reason_guard(setup: CycleSetup, target: str) -> None:
    with tenant_context(setup.tenant.pk):
        cycle = create_cycle(setup)
        with pytest.raises(IllegalTransition):
            transition_cycle(setup.actor, cycle.pk, target, expected_version=1)
        assert not InternalNote.objects.exists()


def test_completion_and_executed_effect_guards_fail_closed(
    setup: CycleSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    with tenant_context(setup.tenant.pk):
        cycle = create_cycle(setup)
        ServiceCycle.objects.filter(pk=cycle.pk).update(state=CycleState.EXECUTION)
        cycle.refresh_from_db()
        assert set(completion_unmet(cycle)) == {"tasks", "evidence_index"}
        with pytest.raises(IllegalTransition):
            transition_cycle(
                setup.actor, cycle.pk, CycleState.COMPLETED, expected_version=1
            )
        monkeypatch.setitem(EFFECT_GUARDS, "delivery", lambda _: True)
        with pytest.raises(IllegalTransition):
            transition_cycle(
                setup.actor, cycle.pk, CycleState.CANCELLED, expected_version=1
            )
        assert CycleEvent.objects.count() == 1


def test_two_callers_cannot_replay_a_cycle_or_note_revision(setup: CycleSetup) -> None:
    with tenant_context(setup.tenant.pk):
        cycle = create_cycle(setup)
        caller_a = ServiceCycle.objects.get(pk=cycle.pk)
        caller_b = ServiceCycle.objects.get(pk=cycle.pk)
        transition_cycle(
            setup.actor, cycle.pk, CycleState.ACTIVE, expected_version=caller_a.version
        )
        before = CycleEvent.objects.count()
        with pytest.raises(VersionConflict):
            transition_cycle(
                setup.actor,
                cycle.pk,
                CycleState.CANCELLED,
                expected_version=caller_b.version,
            )
        with pytest.raises(VersionConflict):
            add_note(setup.actor, cycle.pk, "stale", expected_version=caller_b.version)
        assert CycleEvent.objects.count() == before
        assert not InternalNote.objects.exists()
        add_note(setup.actor, cycle.pk, "secret note", expected_version=2)
        with pytest.raises(VersionConflict):
            add_note(setup.actor, cycle.pk, "losing note", expected_version=2)
        assert InternalNote.objects.count() == 1
        assert "secret note" not in str(
            list(CycleEvent.objects.values_list("payload", flat=True))
        )


def test_task_dependencies_and_stale_caller(setup: CycleSetup) -> None:
    with tenant_context(setup.tenant.pk):
        cycle = create_cycle(setup)
        tasks = {task.step_key: task for task in cycle.tasks.all()}
        steps = list(setup.definition.steps.filter(is_enabled=True))
        enabled = {step.key for step in steps}
        for step in steps:
            assert tasks[step.key].state == (
                TaskState.BLOCKED
                if enabled.intersection(step.depends_on)
                else TaskState.READY
            )
        task = next(task for task in tasks.values() if task.state == TaskState.READY)
        transition_task(setup.actor, task.pk, TaskState.IN_PROGRESS, expected_version=1)
        before = CycleEvent.objects.count()
        with pytest.raises(VersionConflict):
            transition_task(
                setup.actor, task.pk, TaskState.CANCELLED, expected_version=1
            )
        assert CycleEvent.objects.count() == before
        transition_task(setup.actor, task.pk, TaskState.SUBMITTED, expected_version=2)
        transition_task(setup.actor, task.pk, TaskState.ACCEPTED, expected_version=3)
        for other in cycle.tasks.exclude(pk=task.pk):
            # Earlier acceptances release later nodes and increment THEIR revision.
            other.refresh_from_db()
            if other.state in {TaskState.READY, TaskState.BLOCKED}:
                transition_task(
                    setup.actor,
                    other.pk,
                    TaskState.WAIVED,
                    expected_version=other.version,
                    reason="Reviewed waiver",
                )
        assert not cycle.tasks.exclude(
            state__in=[TaskState.ACCEPTED, TaskState.WAIVED]
        ).exists()


def test_unassigned_staff_gets_404_and_zero_writes(setup: CycleSetup) -> None:
    with tenant_context(setup.tenant.pk):
        with pytest.raises(Http404):
            open_cycle(
                setup.staff,
                setup.engagement.pk,
                PERIOD,
                setup.definition.pk,
                idempotency_key="denied",
            )
        assert not ServiceCycle.objects.exists()
        cycle = create_cycle(setup)
        task = cycle.tasks.first()
        assert task is not None
        actions: list[Callable[[], object]] = [
            lambda: transition_cycle(
                setup.staff, cycle.pk, CycleState.ACTIVE, expected_version=1
            ),
            lambda: transition_task(
                setup.staff, task.pk, TaskState.IN_PROGRESS, expected_version=1
            ),
            lambda: add_note(setup.staff, cycle.pk, "denied", expected_version=1),
        ]
        for action in actions:
            with pytest.raises(Http404):
                action()
        assert CycleEvent.objects.count() == 1
        assert not InternalNote.objects.exists()


def test_capability_is_rechecked_after_loading(
    setup: CycleSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    with tenant_context(setup.tenant.pk):
        cycle = create_cycle(setup)
        monkeypatch.setattr(
            "apps.cycles.services.cycle.can", lambda *args, **kwargs: False
        )
        with transaction.atomic(), pytest.raises(PermissionDenied):
            transition_cycle(
                setup.actor, cycle.pk, CycleState.ACTIVE, expected_version=1
            )
        assert CycleEvent.objects.count() == 1
