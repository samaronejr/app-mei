"""AXRM-015 surfaces: the Receita Federal section, its staff actions and Hoje."""

from datetime import date
from decimal import Decimal
from unittest import mock
from uuid import uuid4

import pytest
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.audit.models import Event
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.models import Exception as CycleException
from apps.integrations.models import ClientProviderLink, ProviderConnection
from apps.integrations.serpro import payments as payment_services
from apps.integrations.serpro.models import PaymentQuery
from apps.obligations.models import FederalConsultation, FederalPayment, Obligation
from apps.tenants.models import Tenant
from tests.cycles.cycle_fixtures import PERIOD
from tests.integrations.test_migration_rollback import _as_migrator
from tests.portal.request_fixtures import PortalSetup, portal_setup

pytestmark = pytest.mark.django_db(transaction=True)

SECTION = "data-federal-payments-panel"


@pytest.fixture
def setup(settings: SettingsWrapper) -> PortalSetup:
    settings.SERPRO_ENVIRONMENT = "fake"
    world = portal_setup("pagtoweb-panel")
    tenant = world.graph.tenant
    tenant.features = [*tenant.features, "scan_required", "federal_checks"]
    tenant.save(update_fields=["features"])
    with tenant_context(tenant.pk):
        connection_row = ProviderConnection.objects.create(
            tenant=tenant, provider="serpro", environment="fake", status="ativa"
        )
        ClientProviderLink.objects.create(
            tenant=tenant,
            client=world.graph.client,
            connection=connection_row,
            provider_company_ref="pagtoweb-panel",
            status="ativa",
        )
    return world


def _results(world: PortalSetup, environment: str = "fake") -> PaymentQuery:
    tenant, client = world.graph.tenant, world.graph.client
    with tenant_context(tenant.pk):
        link = ClientProviderLink.objects.get(client=client)
        obligation, _ = Obligation.objects.get_or_create(
            tenant=tenant,
            client=client,
            obligation_type_id="DAS",
            competence_month=PERIOD,
            defaults={
                "nominal_due_date": date(2026, 10, 20),
                "resolved_due_date": date(2026, 10, 20),
            },
        )
        query = PaymentQuery.objects.create(
            tenant=tenant,
            client=client,
            link=link,
            trigger="agendada",
            request_key=uuid4().hex * 2,
            range_start=PERIOD,
            range_end=date(2026, 10, 27),
            environment=environment,
            state="encontrado",
            pages_fetched=1,
            payments_found=2,
            matched_count=1,
        )
        receipt = FederalConsultation.objects.create(
            tenant=tenant,
            client=client,
            kind="pagamentos",
            scope_digest="b" * 64,
            range_start=PERIOD,
            range_end=date(2026, 10, 27),
            consulted_at=query.created_at,
            outcome="encontrado",
            environment=environment,
        )
        for numero, tipo, matched in (
            ("DASPANEL0000001", "DAS", obligation),
            ("DARFPANEL000001", "DARF", None),
        ):
            FederalPayment.objects.create(
                tenant=tenant,
                client=client,
                consultation=receipt,
                numero_documento=numero,
                tipo=tipo,
                periodo_apuracao=PERIOD,
                data_arrecadacao=date(2026, 10, 20),
                receita_codigo="1001",
                valor_total=Decimal("76.60"),
                matched_obligation=matched,
            )
    return query


def _section(html: str) -> str:
    return html.split(SECTION, 1)[1].split("</section>", 1)[0]


def _workspace(world: PortalSetup) -> str:
    return f"/clientes/{world.graph.client.pk}/?periodo={PERIOD:%Y-%m}"


def test_the_section_is_not_drawn_while_federal_checks_is_off(
    setup: PortalSetup,
) -> None:
    tenant = setup.graph.tenant
    tenant.features = [f for f in tenant.features if f != "federal_checks"]
    tenant.save(update_fields=["features"])
    _results(setup)
    browser = setup.firm()
    assert SECTION not in browser.get(_workspace(setup)).content.decode()
    assert SECTION not in browser.get(f"/ciclos/{setup.cycle.pk}/").content.decode()


def test_both_lists_render_with_honest_simulated_labels(setup: PortalSetup) -> None:
    _results(setup)
    browser = setup.firm()
    for url in (_workspace(setup), f"/ciclos/{setup.cycle.pk}/"):
        response = browser.get(url)
        assert response.status_code == 200
        section = _section(response.content.decode())
        assert "Pagamentos associados a obrigações" in section
        assert "Pagamentos encontrados (não associados)" in section
        assert "DASPANEL0000001" in section
        assert "DARFPANEL000001" in section
        assert "R$ 76,60" in section
        assert "Simulado: não confirma pagamento" in section
        assert 'data-state="confirmed"' not in section
        assert "Emitir comprovante" in section


def test_only_production_with_approval_reads_confirmed(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    _results(setup, environment="producao")
    settings.PROVIDER_REAL_DATA_APPROVED = False
    section = _section(setup.firm().get(_workspace(setup)).content.decode())
    assert 'data-state="confirmed"' not in section
    settings.PROVIDER_REAL_DATA_APPROVED = True
    section = _section(setup.firm().get(_workspace(setup)).content.decode())
    assert section.count('data-state="confirmed"') == 1
    assert "Pagamento encontrado na Receita" in section


def test_a_click_queues_one_consultation_and_a_stale_render_conflicts(
    setup: PortalSetup,
) -> None:
    browser = setup.firm()
    form = {"periodo": f"{PERIOD:%Y-%m}", "ultima": "", "nonce": uuid4().hex}
    with mock.patch(
        "apps.integrations.serpro.tasks.run_payment_query_task.delay"
    ) as delay:
        response = browser.post(
            f"/clientes/{setup.graph.client.pk}/receita/pagamentos/consultar/", form
        )
        stale = browser.post(
            f"/clientes/{setup.graph.client.pk}/receita/pagamentos/consultar/",
            {**form, "nonce": uuid4().hex},
        )
    assert response.status_code == 302
    assert response["Location"].endswith("#receita-federal")
    assert stale.status_code == 409
    with tenant_context(setup.graph.tenant.pk):
        [query] = PaymentQuery.objects.all()
    assert (query.state, query.trigger, query.requested_by_id) == (
        "na_fila",
        "equipe",
        setup.graph.actor.pk,
    )
    delay.assert_called_once_with(str(setup.graph.tenant.pk), str(query.pk))


def test_an_accountant_outside_the_portfolio_gets_404_and_no_rows(
    setup: PortalSetup,
) -> None:
    browser = setup.firm(setup.graph.staff)
    response = browser.post(
        f"/clientes/{setup.graph.client.pk}/receita/pagamentos/consultar/",
        {"periodo": f"{PERIOD:%Y-%m}", "ultima": "", "nonce": uuid4().hex},
    )
    assert response.status_code in {403, 404}
    with tenant_context(setup.graph.tenant.pk):
        assert not PaymentQuery.objects.exists()


def test_a_receipt_click_queues_the_task_for_that_payment(setup: PortalSetup) -> None:
    _results(setup)
    with tenant_context(setup.graph.tenant.pk):
        payment = FederalPayment.objects.get(tipo="DAS")
    with mock.patch(
        "apps.integrations.serpro.tasks.emit_payment_receipt_task.delay"
    ) as delay:
        response = setup.firm().post(
            f"/clientes/{setup.graph.client.pk}/receita/pagamentos/{payment.pk}/comprovante/"
        )
    assert response.status_code == 302
    delay.assert_called_once_with(
        str(setup.graph.tenant.pk), str(payment.pk), str(setup.graph.actor.pk)
    )


def _foreign_payment(tenant: Tenant, client: ClientCompany) -> FederalPayment:
    with tenant_context(client.tenant_id):
        receipt = FederalConsultation.objects.create(
            tenant=tenant,
            client=client,
            kind="pagamentos",
            scope_digest="d" * 64,
            range_start=PERIOD,
            range_end=date(2026, 10, 27),
            consulted_at=timezone.now(),
            outcome="encontrado",
            environment="fake",
        )
        return FederalPayment.objects.create(
            tenant=tenant,
            client=client,
            consultation=receipt,
            numero_documento=f"FOREIGN{uuid4().hex[:10]}",
            tipo="DAS",
            periodo_apuracao=PERIOD,
            data_arrecadacao=date(2026, 10, 20),
            receita_codigo="1001",
            valor_total=Decimal("76.60"),
        )


def test_another_clients_payment_is_unreachable_through_this_clients_pk(
    setup: PortalSetup,
) -> None:
    # Given client B's payment, in the same firm the owner can see whole.
    other = _foreign_payment(setup.graph.tenant, setup.other_client)
    client_a = setup.graph.client.pk
    with mock.patch(
        "apps.integrations.serpro.tasks.emit_payment_receipt_task.delay"
    ) as delay:
        # When it is named under client A's pk.
        response = setup.firm().post(
            f"/clientes/{client_a}/receita/pagamentos/{other.pk}/comprovante/"
        )
    # Then the route answers 404 and nothing is queued or recorded.
    assert response.status_code == 404
    delay.assert_not_called()
    with tenant_context(setup.graph.tenant.pk):
        assert not Event.objects.filter(
            action="integrations.payment_receipt_requested"
        ).exists()


def test_another_firms_client_and_payment_are_404_for_both_routes(
    setup: PortalSetup,
) -> None:
    foreign = portal_setup("pagtoweb-panel-foreign")
    payment = _foreign_payment(foreign.graph.tenant, foreign.graph.client)
    browser = setup.firm()
    client_a, client_b = setup.graph.client.pk, foreign.graph.client.pk
    with (
        mock.patch(
            "apps.integrations.serpro.tasks.emit_payment_receipt_task.delay"
        ) as receipt_delay,
        mock.patch(
            "apps.integrations.serpro.tasks.run_payment_query_task.delay"
        ) as query_delay,
    ):
        through_own = browser.post(
            f"/clientes/{client_a}/receita/pagamentos/{payment.pk}/comprovante/"
        )
        through_foreign = browser.post(
            f"/clientes/{client_b}/receita/pagamentos/{payment.pk}/comprovante/"
        )
        consult_foreign = browser.post(
            f"/clientes/{client_b}/receita/pagamentos/consultar/",
            {"periodo": f"{PERIOD:%Y-%m}", "ultima": "", "nonce": uuid4().hex},
        )
    assert (
        through_own.status_code,
        through_foreign.status_code,
        consult_foreign.status_code,
    ) == (404, 404, 404)
    receipt_delay.assert_not_called()
    query_delay.assert_not_called()
    with tenant_context(foreign.graph.tenant.pk):
        assert not PaymentQuery.objects.exists()


def test_a_failed_consultation_is_a_hoje_pendencia_of_the_open_cycle(
    setup: PortalSetup,
) -> None:
    tenant = setup.graph.tenant
    tenant.features = [*tenant.features, "briefing_r1"]
    tenant.save(update_fields=["features"])
    query = _results(setup)
    with tenant_context(tenant.pk):
        locked = PaymentQuery.objects.select_related("client").get(pk=query.pk)
        payment_services._finish(locked, "falha", "limite_de_paginas")
        problem = CycleException.objects.get(
            cycle=setup.cycle, reason_code="consulta_pagamentos_falhou"
        )
    assert problem.subject_ref == query.pk
    page = setup.firm().get("/").context["page"]
    titles = [item.title for item in page.items if item.kind == "exception"]
    assert "consulta de pagamentos na Receita com falha" in titles


def test_the_migration_reverses_empty_and_refuses_once_populated(
    setup: PortalSetup,
) -> None:
    with _as_migrator():
        MigrationExecutor(connection).migrate(
            [("integrations", "0006_firm_provider_reservations")]
        )
        MigrationExecutor(connection).migrate(
            [("integrations", "0007_payment_queries")]
        )
    _results(setup)
    with _as_migrator(), pytest.raises(IrreversibleError):
        MigrationExecutor(connection).migrate(
            [("integrations", "0006_firm_provider_reservations")]
        )
    with tenant_context(setup.graph.tenant.pk):
        assert PaymentQuery.objects.count() == 1
