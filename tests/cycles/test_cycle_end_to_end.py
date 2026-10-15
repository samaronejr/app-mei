"""Imported client to immutable completion through the actual service boundaries."""

from pathlib import Path

import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.models import ClientAcknowledgement, CycleTask, RequestState, TaskState
from apps.delivery.models import DeliveryEffect, MessageState, OutboundMessage
from tests.cycles.evidence_fixtures import scenario_a

pytestmark = pytest.mark.django_db(transaction=True)


def test_import_upload_review_approval_execution_receipt_and_completion(
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    scenario = scenario_a(settings, tmp_path, "completion-scenario-a")
    assert scenario.cycle_states == (
        "planejado",
        "em_andamento",
        "aguardando_cliente",
        "em_andamento",
        "em_revisao",
        "aguardando_aprovacao",
        "em_execucao",
        "concluido",
    )
    assert scenario.request_versions == (1, 2, 3)
    with tenant_context(scenario.setup.graph.tenant.pk):
        scenario.setup.request.refresh_from_db()
        assert scenario.setup.graph.client.import_batch_id is not None
        assert scenario.setup.request.state == RequestState.ACCEPTED
        assert (
            not CycleTask.objects.filter(cycle=scenario.setup.cycle)
            .exclude(state=TaskState.ACCEPTED)
            .exists()
        )
        assert (
            OutboundMessage.objects.filter(
                cycle=scenario.setup.cycle, state=MessageState.ACCEPTED
            ).count()
            == 2
        )
        assert DeliveryEffect.objects.count() == 2
        assert ClientAcknowledgement.objects.count() == 1
    assert scenario.index.data["cycle"]["state"] == "concluido"
    assert len(scenario.index.data["executions"]) == 2
