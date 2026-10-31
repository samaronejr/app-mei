"""AXRM-015's Receita Federal section at 1440x900 and 390x844: lists, labels, click."""

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from playwright.sync_api import expect
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.integrations.models import ClientProviderLink, ProviderConnection
from apps.integrations.serpro.models import PaymentQuery
from apps.obligations.models import FederalConsultation, FederalPayment, Obligation
from tests.browser.harness import BrowserPage, LiveFirm, axe_check
from tests.ui.factories import add_client

FLAGS = ("cycles_r1", "scan_required", "federal_checks")
PERIOD = date(2026, 9, 1)


def _seed(live: LiveFirm) -> str:
    tenant = live.firm.tenant
    company = add_client(
        live.firm, legal_name="Cliente MEI demonstrativo", base="112223330003"
    )
    with tenant_context(tenant.pk):
        connection = ProviderConnection.objects.create(
            tenant=tenant, provider="serpro", environment="fake", status="ativa"
        )
        link = ClientProviderLink.objects.create(
            tenant=tenant,
            client=company,
            connection=connection,
            provider_company_ref="browser-pagtoweb",
            status="ativa",
        )
        obligation = Obligation.objects.create(
            tenant=tenant,
            client=company,
            obligation_type_id="DAS",
            competence_month=PERIOD,
            nominal_due_date=date(2026, 10, 20),
            resolved_due_date=date(2026, 10, 20),
        )
        query = PaymentQuery.objects.create(
            tenant=tenant,
            client=company,
            link=link,
            trigger="agendada",
            request_key=uuid4().hex * 2,
            range_start=PERIOD,
            range_end=date(2026, 10, 27),
            environment="fake",
            state="encontrado",
            pages_fetched=2,
            payments_found=103,
            matched_count=1,
        )
        receipt = FederalConsultation.objects.create(
            tenant=tenant,
            client=company,
            kind="pagamentos",
            scope_digest="c" * 64,
            range_start=PERIOD,
            range_end=date(2026, 10, 27),
            consulted_at=query.created_at,
            outcome="encontrado",
            environment="fake",
        )
        for numero, tipo, matched in (
            ("07082207696788000", "DAS", obligation),
            ("DARF000000000001", "DARF", None),
        ):
            FederalPayment.objects.create(
                tenant=tenant,
                client=company,
                consultation=receipt,
                numero_documento=numero,
                tipo=tipo,
                periodo_apuracao=PERIOD,
                data_arrecadacao=date(2026, 10, 20),
                receita_codigo="1001",
                valor_total=Decimal("1234.56"),
                matched_obligation=matched,
            )
    return f"/clientes/{company.pk}/?periodo={PERIOD:%Y-%m}"


def test_the_receita_federal_section_at_both_widths(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
    settings: SettingsWrapper,
) -> None:
    settings.SERPRO_ENVIRONMENT = "fake"
    live = live_firm("browser-pagtoweb", features=FLAGS)
    path = _seed(live)
    opened = open_page(live, None)
    opened.goto(live.url(path))
    panel = opened.page.locator("[data-federal-payments-panel]")
    expect(panel).to_be_visible()
    expect(
        panel.get_by_role("heading", name="Pagamentos encontrados (não associados)")
    ).to_be_visible()
    matched = panel.locator("[data-federal-payment]").filter(
        has_text="07082207696788000"
    )
    expect(matched).to_be_visible()
    expect(
        matched.get_by_text("Simulado: não confirma pagamento", exact=False)
    ).to_be_visible()
    expect(panel.get_by_text("R$ 1.234,56").first).to_be_visible()
    expect(panel.locator('[data-state="confirmed"]')).to_have_count(0)
    overflow = opened.page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 0
    panel.scroll_into_view_if_needed()
    opened.screenshot("AXRM-015-panel-results")
    axe_check(opened.page)
    with mock.patch(
        "apps.integrations.serpro.tasks.run_payment_query_task.delay"
    ) as delay:
        panel.get_by_role("button", name="Consultar pagamentos desde set/2026").click()
        expect(
            opened.page.locator("[data-federal-running]").get_by_text(
                "Consulta em andamento"
            )
        ).to_be_visible()
    assert delay.call_count == 1
    expect(
        panel.get_by_role("button", name="Consultar pagamentos desde set/2026")
    ).to_have_count(0)
    opened.screenshot("AXRM-015-panel-queued")
    axe_check(opened.page)
    opened.assert_clean_console()
