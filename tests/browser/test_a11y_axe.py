"""axe-core on the R1 screens of S5.5 in their main states, at 1440x900 and 390x844.

One seeded firm carries every state the sweep needs: a client with an open cycle, a
sent request, a reviewed document and an approval awaiting decision; a client with
no service; a portal seat. Each screen is opened, axe runs, and a screenshot is
kept. Every serious or critical violation on every screen is collected before the
test fails, so one run names them all.

States that need a whole journey are swept there, through the same `record` step:
the invalidated and stale-409 approval card (test_journey_d_q), SMTP down and the
cancelled reminder (test_journey_i), the divergent document (test_journey_b), the
offline upload and its retry (test_upload_retry_slow_network), publication and
acknowledgement (test_journey_a), the support banner (test_journey_j). Automações
(T27) and Suporte (T23, with a request awaiting the owner) are swept here, and so
are the portal vault, with a served document and empty, and the account page (T45).

An axe pass is not a WCAG claim. The manual NVDA and VoiceOver passes in
ops/AXORIM-A11Y-CHECKLIST.md are the operator's, and that gate stays open.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from django.urls import reverse
from django.utils import timezone
from playwright.sync_api import expect
from pytest_django.fixtures import SettingsWrapper
from pytest_django.live_server_helper import LiveServer

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.models import ActionProposal, EvidenceLink, ServiceCycle
from apps.cycles.services.approvals import propose
from apps.tenants.support import request_support_grant
from tests.browser.harness import (
    AxeViolationsError,
    BrowserPage,
    LiveFirm,
    assert_policed_roles,
)
from tests.browser.journeys import R1_FLAGS, live_graph, open_portal, record
from tests.cycles.document_fixtures import reviewed
from tests.portal.request_fixtures import PortalSetup, portal_setup
from tests.ui.factories import make_cnpj

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]


@dataclass(frozen=True)
class World:
    setup: PortalSetup
    live: LiveFirm
    link: EvidenceLink
    proposal: ActionProposal
    unserved: ClientCompany

    @property
    def period(self) -> str:
        return f"{self.setup.cycle.period_start:%Y-%m}"

    def firm_screens(self) -> dict[str, str]:
        client = self.setup.graph.client.pk
        definition = self.setup.graph.definition
        return {
            "hoje": f"/?escopo=team&periodo={self.period}",
            "clientes": reverse("clients-list"),
            "clientes-filtro-vazio": f"{reverse('clients-list')}?q=sem-resultado",
            "importar": reverse("client-import"),
            "cliente-ciclo": f"/clientes/{client}/?periodo={self.period}",
            "cliente-sem-servico": (
                f"/clientes/{self.unserved.pk}/?periodo={self.period}"
            ),
            "ciclo": f"/ciclos/{self.setup.cycle.pk}/",
            "indice-evidencias": reverse("evidence-index", args=[self.setup.cycle.pk]),
            "playbooks": reverse("playbook-list"),
            "playbook-editor": reverse(
                "playbook-editor", args=[definition.playbook_id, definition.number]
            ),
            "documentos": reverse("document-queue"),
            "documento-revisao": f"/documentos/{self.link.pk}/revisar/",
            "aprovacao": f"/acoes/{self.proposal.pk}/",
            "comunicacao": reverse("delivery-queue"),
            "configuracoes": reverse("firm-settings"),
            "configuracoes-conta": reverse("firm-account"),
            "automacoes": reverse("automations"),
            "suporte": reverse("firm-suporte"),
        }

    def portal_screens(self) -> dict[str, str]:
        return {
            "portal-inicio": "/",
            "portal-solicitacao": f"/solicitacoes/{self.setup.request.pk}/",
        }


@pytest.fixture
def world(
    browser_server: LiveServer, settings: SettingsWrapper, tmp_path: Path
) -> World:
    settings.MEDIA_ROOT = tmp_path
    setup = portal_setup("axe-sweep")
    live = live_graph(
        setup.graph,
        browser_server.thread.port,
        *R1_FLAGS,
        "firm_settings_r1",
        "import_r1",
    )
    _, link = reviewed(setup, "nfe_emitente_cliente.xml")
    with tenant_context(setup.graph.tenant.pk):
        cycle = ServiceCycle.objects.get(pk=setup.cycle.pk)
        proposal = propose(
            setup.graph.actor,
            cycle.pk,
            ProposalDraft(
                ProposalInput(
                    action_kind=ActionKind.EMAIL_NOTICE,
                    step_key="aviso",
                    recipient_ids=(setup.contact.pk,),
                    subject="Documentos de setembro",
                    body="Confira os documentos selecionados.",
                ),
                timezone.now() + timedelta(days=1),
            ),
            expected_version=cycle.version,
        )
        request_support_grant(
            User.objects.create_user(email="suporte@axe.example.com", is_staff=True),
            setup.graph.tenant.pk,
            purpose="Conferir a fila de comunicação",
        )
        unserved = ClientCompany.objects.create(
            tenant=setup.graph.tenant,
            legal_name="Cliente sem serviço contratado",
            cnpj=make_cnpj("556677880001"),
            status="active",
        )
    return World(setup, live, link, proposal, unserved)


def _sweep(
    page: BrowserPage, live: LiveFirm, screens: dict[str, str], *, portal: bool
) -> dict[str, str]:
    failures: dict[str, str] = {}
    for name, path in screens.items():
        page.goto(live.url(path, portal=portal))
        expect(page.page.locator("h1")).to_have_count(1)
        try:
            record(page, f"axe-{name}")
        except AxeViolationsError as violation:
            failures[name] = str(violation)
    return failures


def _axe_into(failures: dict[str, str], page: BrowserPage, name: str) -> None:
    try:
        record(page, f"axe-{name}")
    except AxeViolationsError as violation:
        failures[name] = str(violation)


def test_axe_on_every_firm_screen(world: World, open_page: OpenPage) -> None:
    owner = open_page(world.live, world.setup.graph.actor)
    failures = _sweep(owner, world.live, world.firm_screens(), portal=False)
    assert failures == {}, "\n\n".join(f"{k}: {v}" for k, v in failures.items())
    owner.assert_clean_console()
    assert_policed_roles()


def test_axe_on_every_portal_screen(world: World, open_page: OpenPage) -> None:
    portal = open_portal(open_page, world.live, world.setup.portal_user)
    failures = _sweep(portal, world.live, world.portal_screens(), portal=True)
    assert failures == {}, "\n\n".join(f"{k}: {v}" for k, v in failures.items())
    portal.assert_clean_console()
    assert_policed_roles()


def test_axe_on_the_portal_vault_and_account(world: World, open_page: OpenPage) -> None:
    portal = open_portal(open_page, world.live, world.setup.portal_user)
    failures: dict[str, str] = {}

    portal.goto(world.live.url("/documentos/", portal=True))
    expect(portal.page.get_by_text("nfe_emitente_cliente.xml")).to_be_visible()
    _axe_into(failures, portal, "portal-documentos")

    portal.goto(world.live.url("/conta/", portal=True))
    expect(portal.page.locator("main dd")).to_have_text(world.setup.portal_user.email)
    _axe_into(failures, portal, "portal-conta")

    assert failures == {}, "\n\n".join(f"{k}: {v}" for k, v in failures.items())
    portal.assert_clean_console()
    assert_policed_roles()


def test_axe_on_the_empty_portal_vault(
    browser_server: LiveServer, open_page: OpenPage
) -> None:
    setup = portal_setup("axe-cofre-vazio")
    live = live_graph(setup.graph, browser_server.thread.port, *R1_FLAGS)
    portal = open_portal(open_page, live, setup.portal_user)

    portal.goto(live.url("/documentos/", portal=True))
    expect(portal.page.locator("main li.card")).to_have_count(0)
    expect(portal.page.locator("main .empty-state")).to_be_visible()
    record(portal, "axe-portal-documentos-vazio")

    portal.assert_clean_console()
    assert_policed_roles()


def test_axe_on_the_empty_and_forbidden_states(
    world: World, open_page: OpenPage
) -> None:
    staff = open_page(world.live, world.setup.graph.staff)
    staff.goto(world.live.url(f"/?escopo=me&periodo={world.period}"))
    expect(staff.page.locator("[data-briefing-item]")).to_have_count(0)
    record(staff, "axe-hoje-vazio")

    denied = staff.page.goto(world.live.url(reverse("approval-policy")))
    assert denied is not None
    assert denied.status == 403
    expect(staff.page.locator("html")).to_have_attribute("lang", "pt-br")
    expect(staff.page.locator("[data-refusal-reason]")).to_be_visible()
    body = staff.page.locator("body").inner_text()
    assert world.setup.graph.client.legal_name not in body
    record(staff, "axe-proibido")
    assert_policed_roles()
    assert all("status of 403" in error for error in staff.console_errors), (
        staff.console_errors
    )
