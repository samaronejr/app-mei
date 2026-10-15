"""Old cycle forms must propose consequential effects instead of bypassing approval."""

import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.models import ActionProposal, CycleEvent, RequestState, ServiceCycle
from apps.cycles.services.requests import RequestFields, create_request
from tests.cycles.publication_fixtures import publication_graph

pytestmark = pytest.mark.django_db(transaction=True)


def test_existing_waiver_form_creates_approval_without_waiving_request(
    settings: SettingsWrapper,
) -> None:
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    setup = publication_graph()
    with tenant_context(setup.graph.tenant.pk):
        request = create_request(
            setup.graph.actor,
            setup.cycle.pk,
            RequestFields("Comprovante pendente"),
            expected_version=setup.cycle.version,
        )
    response = setup.session(portal=False).post(
        f"/ciclos/{setup.cycle.pk}/solicitacoes/",
        {
            "action": "waive",
            "request_id": request.pk,
            "expected_version": request.version,
            "reason": "Motivo para aprovação independente.",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/acoes/")
    with tenant_context(setup.graph.tenant.pk):
        request.refresh_from_db()
        assert request.state == RequestState.DRAFT
        proposal = ActionProposal.objects.get(action_kind="request_waiver")
        assert proposal.state == "aguardando_aprovacao"
        assert proposal.input_spec["evidence_requests"] == [str(request.pk)]


@pytest.mark.parametrize("state", ["concluido", "encerrado_com_pendencias"])
def test_existing_completion_form_freezes_target_and_stale_post_writes_nothing(
    settings: SettingsWrapper, state: str
) -> None:
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    setup = publication_graph()
    with tenant_context(setup.graph.tenant.pk):
        ServiceCycle.objects.filter(pk=setup.cycle.pk).update(state="em_execucao")
    form = {
        "action": "transition",
        "to_state": state,
        "expected_version": setup.cycle.version,
        "reason": "Motivo de encerramento.",
    }
    desk = setup.session(portal=False)
    url = f"/ciclos/{setup.cycle.pk}/"
    response = desk.post(url, form)
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/acoes/")
    with tenant_context(setup.graph.tenant.pk):
        setup.cycle.refresh_from_db()
        assert setup.cycle.state == "em_execucao"
        proposal = ActionProposal.objects.get(action_kind="cycle_completion")
        assert proposal.input_spec["completion_state"] == state
        assert proposal.state == "aguardando_aprovacao"
        count = ActionProposal.objects.count()
    assert desk.post(url, form).status_code == 409
    with tenant_context(setup.graph.tenant.pk):
        assert ActionProposal.objects.count() == count


@pytest.mark.parametrize("action", ["waive", "complete"])
def test_consequential_form_refuses_closed_context_without_server_error(
    settings: SettingsWrapper, action: str
) -> None:
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    setup = publication_graph()
    with tenant_context(setup.graph.tenant.pk):
        pending = create_request(
            setup.graph.actor,
            setup.cycle.pk,
            RequestFields("Comprovante pendente"),
            expected_version=setup.cycle.version,
        )
        setup.cycle.refresh_from_db()
        ServiceCycle.objects.filter(pk=setup.cycle.pk).update(state="cancelado")
        before = ActionProposal.objects.count(), CycleEvent.objects.count()
    url = f"/ciclos/{setup.cycle.pk}/"
    form = {
        "action": "transition",
        "to_state": "concluido",
        "expected_version": setup.cycle.version,
        "reason": "Proposta de uma tela já encerrada.",
    }
    if action == "waive":
        url += "solicitacoes/"
        form.update(
            action="waive",
            request_id=str(pending.pk),
            expected_version=pending.version,
        )
    desk = setup.session(portal=False)
    desk.raise_request_exception = False
    response = desk.post(url, form)
    assert response.status_code == 422
    with tenant_context(setup.graph.tenant.pk):
        assert (ActionProposal.objects.count(), CycleEvent.objects.count()) == before
