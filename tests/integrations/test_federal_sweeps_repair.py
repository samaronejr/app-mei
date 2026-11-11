"""AXRM-018 review regressions: recover handoffs and project one owned problem."""

import pytest
from django.utils import timezone

from apps.briefing import providers
from apps.briefing.source_rows import SourceContext
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleState, ServiceCycle
from apps.integrations.authority import Problem, authority_subject, raise_problem
from apps.integrations.models import IntegrationIssue
from apps.integrations.serpro import sitfis_tasks
from apps.obligations.models import FiscalSituationReport
from tests.integrations.sitfis_support import World, world
from tests.integrations.test_federal_sweeps_sitfis import DAY, clock, fire

__all__ = ["clock", "world"]
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("failure", ["broker_down", "worker_killed"])
def test_lost_handoff_is_delivered_again(
    world: World, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    delivered: list[tuple[str, str]] = []
    broken = [True]

    def delay(tenant_id: str, report_id: str) -> None:
        if broken[0]:
            broken[0] = False
            if failure == "broker_down":
                message = "broker down"
                raise ConnectionError(message)
            raise SystemExit
        delivered.append((tenant_id, report_id))

    monkeypatch.setattr(sitfis_tasks.advance_situation_report_task, "delay", delay)
    if failure == "worker_killed":
        with pytest.raises(SystemExit):
            fire(monkeypatch)
    else:
        fire(monkeypatch)
    with tenant_context(world.tenant_id):
        report = FiscalSituationReport.objects.get()
        assert report.state == "solicitado"
        assert report.emission_attempts == 0
    assert fire(monkeypatch, DAY.replace(day=16)) == 1
    assert delivered == [(str(world.tenant_id), str(report.pk))]
    with tenant_context(world.tenant_id):
        assert FiscalSituationReport.objects.count() == 1
        report.refresh_from_db()
        assert report.emission_attempts == 0


def test_one_problem_uses_only_the_open_cycle_source(world: World) -> None:
    client = world.termo.client
    with tenant_context(world.tenant_id):
        IntegrationIssue.objects.create(
            tenant_id=world.tenant_id,
            client=client,
            reason="autorizacao_ausente",
            service="pagtoweb",
            owner=world.termo.actor,
            review_on=DAY,
        )
        raise_problem(
            client,
            Problem(
                "autorizacao_ausente",
                authority_subject(client.pk, "pagtoweb"),
                review_on=DAY,
                service="pagtoweb",
            ),
        )
        context = SourceContext(clients=ClientCompany.objects.all(), now=timezone.now())
        kinds = [
            item.kind
            for item in providers.source_items(context)
            if item.client_id == client.pk
        ]
        assert kinds == ["exception"]


def test_resolved_issue_leaves_hoje(world: World) -> None:
    with tenant_context(world.tenant_id):
        ServiceCycle.objects.update(state=CycleState.COMPLETED)
        issue = IntegrationIssue.objects.create(
            tenant_id=world.tenant_id,
            client=world.termo.client,
            reason="autorizacao_ausente",
            service="pagtoweb",
            owner=world.termo.actor,
            review_on=DAY,
        )
        context = SourceContext(clients=ClientCompany.objects.all(), now=timezone.now())
        assert any(
            item.kind == "integration" for item in providers.source_items(context)
        )
        issue.state = "resolvida"
        issue.save(update_fields=["state"])
        assert not any(
            item.kind == "integration" for item in providers.source_items(context)
        )


def test_integration_items_stay_in_the_selected_portfolio(world: World) -> None:
    with tenant_context(world.tenant_id):
        ServiceCycle.objects.update(state=CycleState.COMPLETED)
        other = ClientCompany.objects.create(
            tenant_id=world.tenant_id,
            legal_name="Other synthetic client",
            cnpj="12345678000195",
        )
        for client in (world.termo.client, other):
            IntegrationIssue.objects.create(
                tenant_id=world.tenant_id,
                client=client,
                reason="autorizacao_ausente",
                service="pagtoweb",
                owner=world.termo.actor,
                review_on=DAY,
            )
        context = SourceContext(
            clients=ClientCompany.objects.filter(pk=world.termo.client.pk),
            now=timezone.now(),
        )
        items = [
            item
            for item in providers.source_items(context)
            if item.kind == "integration"
        ]
        assert [item.client_id for item in items] == [world.termo.client.pk]
