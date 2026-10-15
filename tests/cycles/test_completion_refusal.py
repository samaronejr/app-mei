"""An approved completion names the state and the real path to reach it."""

from http import HTTPStatus

import pytest
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.models import ActionExecution, CycleEvent, CycleState, ServiceCycle
from tests.browser.harness import mfa_session_cookie
from tests.cycles.completion_fixtures import approved_completion, completion_setup
from tests.support import enrol_totp
from tests.ui.refusal_contract import assert_readable_page

pytestmark = pytest.mark.django_db(transaction=True)


def test_approved_completion_refusal_names_execution_and_next_action(
    settings: SettingsWrapper,
) -> None:
    settings.ALLOWED_HOSTS = [".localhost", "localhost", "testserver"]
    setup = completion_setup("completion-refusal")
    host = f"{setup.graph.tenant.slug}.localhost"
    session = Client(SERVER_NAME=host)
    enrol_totp(setup.graph.actor)
    cookie = mfa_session_cookie(setup.graph.actor, f"http://{host}/")
    session.cookies[cookie["name"]] = cookie["value"]
    with tenant_context(setup.graph.tenant.pk):
        ServiceCycle.objects.filter(pk=setup.cycle.pk).update(state=CycleState.ACTIVE)
        proposal = approved_completion(setup)
        before = CycleEvent.objects.count()

    response = session.post(
        f"/acoes/{proposal.pk}/",
        {"action": "execute", "expected_version": proposal.version},
    )

    body = assert_readable_page(
        response,
        status=HTTPStatus.UNPROCESSABLE_CONTENT,
        reason='Para concluir, o ciclo precisa estar em "Em execução".',
    )
    assert "Volte ao ciclo" in body
    assert "Próximo estado" in body
    assert "Em revisão" in body
    assert "Aguardando aprovação" in body
    assert f'href="/ciclos/{setup.cycle.pk}/"' in body
    with tenant_context(setup.graph.tenant.pk):
        assert ServiceCycle.objects.get(pk=setup.cycle.pk).state == CycleState.ACTIVE
        assert not ActionExecution.objects.filter(proposal=proposal).exists()
        assert CycleEvent.objects.count() == before
