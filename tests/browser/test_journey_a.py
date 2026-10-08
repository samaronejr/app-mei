"""S15 A, the golden journey, across a staff browser, an owner browser and a client one.

Given an owner imported a client and set its MEI profile and engagement, the period's
cycle was generated, its document request went out and is now overdue (so Hoje
lists it, with the overdue-request exception), the journey runs from Hoje:

1. the staff member opens the cycle from its Hoje item; the worker hands the request
   e-mail to the fake SMTP peer and Comunicação reports the server's acceptance,
   never delivery; the cycle waits on the client;
2. the client finds the request in the portal and uploads the NF-e XML;
3. the staff member reviews and accepts the document, resolves the exception, and
   proposes the client publication with the exact frozen effects;
4. the owner approves and executes it; the worker sends the notice; the client opens
   the publication and acknowledges the exact bytes;
5. the staff member captures the evidence index and proposes completion; the owner
   approves and executes it; the cycle is Concluído, the index lists every item,
   and the portal shows the client's next step without any internal note.

Worker steps (dispatch, scan verdict, extraction) run through the real services on
the test thread. Internal task bookkeeping (each playbook task through its three
states) runs through the same service the scenario twin uses,
`tests/cycles/test_cycle_end_to_end.py`; every decision a person makes is a click.

Both viewports. No fixed sleep: every wait is an auto-waiting `expect` or a response.
"""

from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from django.utils import timezone
from playwright.sync_api import FilePayload, Page, expect
from pytest_django.fixtures import SettingsWrapper
from pytest_django.live_server_helper import LiveServer

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake, EvidenceLink
from apps.cycles.models import (
    ClientAcknowledgement,
    ClientPublication,
    CycleState,
    ExceptionState,
    ServiceCycle,
)
from apps.cycles.openers import REQUEST_OVERDUE
from apps.cycles.services.exceptions import open_exception, reason_for
from apps.cycles.services.requests import send_requests
from apps.delivery.models import MessageState
from apps.delivery.tasks import dispatch_tenant
from apps.obligations.generator import generate_das_calendar
from apps.obligations.models import Obligation, SourceObservation
from tests.browser.harness import BrowserPage, LiveFirm, assert_policed_roles
from tests.browser.journeys import R1_FLAGS, live_graph, open_portal, record, smtp_at
from tests.cycles.document_fixtures import extract, fixture, scan_clean
from tests.cycles.evidence_fixtures import accept_tasks, advance, imported_setup
from tests.delivery.smtp import FakeSMTP
from tests.portal.request_fixtures import PortalSetup

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]
XML_NAME = "nota-do-periodo.xml"
PRIVATE = "PRIVATE-SCENARIO-A-INSTRUCTIONS"


def _given(setup: PortalSetup) -> None:
    with tenant_context(setup.graph.tenant.pk):
        advance(setup, CycleState.ACTIVE)
        setup.cycle.refresh_from_db()
        send_requests(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )
        setup.cycle.refresh_from_db()
        open_exception(
            REQUEST_OVERDUE,
            setup.request.pk,
            actor=setup.graph.actor,
            cycle_id=setup.cycle.pk,
            expected_version=setup.cycle.version,
            owner=setup.graph.staff,
            review_on=timezone.localdate(),
            evidence_refs=(setup.request.pk,),
        )


def _post(page: Page, fragment: str, click: Callable[[], None]) -> int:
    with page.expect_response(
        lambda response: response.request.method == "POST" and fragment in response.url
    ) as posted:
        click()
    return posted.value.status


def _cycle_state(staff: BrowserPage, setup: PortalSetup, state: str) -> None:
    page = staff.page
    page.locator("#target").select_option(state)
    status = _post(
        page,
        f"/ciclos/{setup.cycle.pk}/",
        lambda: page.get_by_role("button", name="Alterar estado").click(),
    )
    assert status == 302, f"moving the cycle to {state} answered {status}"
    expect(page.locator("#cycle-summary")).to_contain_text(str(CycleState(state).label))


def _approve_and_execute(owner: BrowserPage, live: LiveFirm, proposal_url: str) -> None:
    owner.goto(proposal_url)
    page = owner.page
    assert (
        _post(
            page,
            proposal_url.split(str(live.port))[1],
            lambda: page.locator('button[value="approve"]').click(),
        )
        == 302
    )
    expect(page.locator("[data-proposal-state]")).to_have_attribute(
        "data-proposal-state", "aprovada"
    )


def test_journey_a_from_hoje_to_a_completed_cycle(  # noqa: PLR0915 - one journey, end to end
    browser_server: LiveServer,
    open_page: OpenPage,
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    settings.DELIVERY_PAUSED = False
    setup = imported_setup("journey-a")
    _given(setup)
    live = live_graph(setup.graph, browser_server.thread.port, *R1_FLAGS)
    live.firm.clients = [setup.graph.client]
    staff = open_page(live, setup.graph.staff)
    owner = open_page(live, setup.graph.actor)
    cycle_url = live.url(f"/ciclos/{setup.cycle.pk}/")

    # 1. Hoje -> cycle; the request e-mail leaves; the cycle waits on the client.
    staff.goto(live.url(f"/?escopo=team&periodo={setup.cycle.period_start:%Y-%m}"))
    item = staff.page.locator(f'[data-briefing-item="request:{setup.request.pk}"]')
    expect(item).to_contain_text(setup.request.title)
    record(staff, "a-01-hoje")
    item.get_by_role("link", name="Revisar solicitação").click()
    expect(staff.page).to_have_url(cycle_url)
    row = staff.page.locator(f"#solicitacao-{setup.request.pk}")
    expect(row).to_contain_text("Atrasada")
    record(staff, "a-02-cycle-overdue")

    with FakeSMTP(mode="accept") as peer, smtp_at(peer.port):
        assert dispatch_tenant(str(setup.graph.tenant.pk)) == 1
        assert len(peer.received) == 1
        staff.goto(live.url("/comunicacao/"))
        sent = staff.page.locator(f'[data-state="{MessageState.ACCEPTED}"]')
        expect(sent).to_have_count(1)
        expect(staff.page.locator("main")).to_contain_text(
            "Aceitação pelo servidor não confirma a leitura."
        )
        expect(staff.page.locator("main")).not_to_contain_text("entregue")
        record(staff, "a-03-comunicacao-accepted")

        staff.goto(cycle_url)
        _cycle_state(staff, setup, CycleState.WAITING)

        # 2. The client finds the request and uploads the NF-e.
        client = open_portal(open_page, live, setup.portal_user)
        client.goto(live.url("/", portal=True))
        expect(
            client.page.locator(f"[data-request-id='{setup.request.pk}']")
        ).to_be_visible()
        record(client, "a-04-portal-pending")
        client.page.get_by_role("link", name=setup.request.title).click()
        # The upload control is HTMX + Alpine: send only once they are attached.
        client.page.wait_for_load_state("load")
        expect(client.page.locator("h1")).to_have_text(setup.request.title)
        payload = fixture("nfe_emitente_cliente.xml").replace(
            b"11222333000181", setup.graph.client.cnpj.encode()
        )
        client.page.set_input_files(
            "#arquivo",
            files=[
                FilePayload(name=XML_NAME, mimeType="application/xml", buffer=payload)
            ],
        )
        assert (
            _post(
                client.page,
                f"/solicitacoes/{setup.request.pk}/enviar",
                lambda: client.page.get_by_role(
                    "button", name="Enviar arquivo"
                ).click(),
            )
            == 200
        )
        expect(client.page.locator("#request-status .badge")).to_have_text("Recebido")
        record(client, "a-05-portal-recebido")

        with tenant_context(setup.graph.tenant.pk):
            intake = DocumentIntake.objects.get(document__original_filename=XML_NAME)
        scan_clean(setup, intake)
        extract(setup, intake)
        with tenant_context(setup.graph.tenant.pk):
            link = EvidenceLink.objects.get(document_id=intake.document_id)

        # 3. Review, the exception, the facts, the proposal.
        staff.goto(cycle_url)
        expect(row).to_contain_text("Recebido do cliente · aguardando conferência")
        _cycle_state(staff, setup, CycleState.ACTIVE)
        _cycle_state(staff, setup, CycleState.REVIEW)
        staff.goto(live.url(f"/documentos/{link.pk}/revisar/"))
        record(staff, "a-06-review")
        assert (
            _post(
                staff.page,
                f"/documentos/{link.pk}/revisar/",
                lambda: staff.page.get_by_role(
                    "button", name="Aceitar documento"
                ).click(),
            )
            == 302
        )

        staff.goto(cycle_url)
        reason = reason_for(REQUEST_OVERDUE)
        assert reason is not None
        staff.page.get_by_role("link", name=f"Pendência: {reason.label}.").click()
        dialog = staff.page.get_by_role("dialog")
        expect(dialog).to_be_visible()
        dialog.get_by_role("button", name="Começar a tratar", exact=True).click()
        expect(dialog.locator("[data-state]")).to_have_attribute(
            "data-state", ExceptionState.HANDLING
        )
        dialog.get_by_label("O que foi feito ou motivo").fill(
            "XML recebido e conferido."
        )
        dialog.get_by_role("button", name="Marcar como resolvida", exact=True).click()
        expect(dialog.locator("[data-state]")).to_have_attribute(
            "data-state", ExceptionState.RESOLVED
        )
        record(staff, "a-07-exception-resolved")

        with tenant_context(setup.graph.tenant.pk):
            generate_das_calendar(
                setup.graph.client, starting_from=setup.cycle.period_start
            )
            subject = Obligation.objects.get(
                client=setup.graph.client, competence_month=setup.cycle.period_start
            )
            observation = SourceObservation.objects.create(
                tenant=setup.graph.tenant,
                client=setup.graph.client,
                subject_ref=subject,
                period=setup.cycle.period_start,
                kind="guide_issued",
                source_kind="staff_recorded",
                verification_status="unknown",
                observed_at=timezone.now(),
                source_ref=f"SCOPED-SOURCE-{setup.graph.client.pk}",
            )
            setup.cycle.refresh_from_db()
            accept_tasks(setup)
        staff.goto(cycle_url)
        _cycle_state(staff, setup, CycleState.APPROVAL)

        staff.page.get_by_role("link", name="Propor efeitos para aprovação").click()
        form = staff.page.locator("[data-proposal-form]")
        form.get_by_label("Ação", exact=True).select_option("client_publication")
        form.get_by_label("Etapa", exact=True).fill("cenario-a")
        for name, value in (
            ("recipients", setup.contact.pk),
            ("document_links", link.pk),
            ("evidence_requests", setup.request.pk),
            ("observations", observation.pk),
        ):
            form.locator(f'input[name="{name}"][value="{value}"]').check()
        form.get_by_label("Assunto", exact=True).fill("Evidência conferida")
        form.get_by_label("Mensagem completa", exact=True).fill(
            "Confira a nota do período."
        )
        form.get_by_role("button", name="Congelar efeitos e conferir cartão").click()
        card = staff.page.locator("[data-approval-card]")
        expect(card).to_be_visible()
        proposal_id = card.get_attribute("data-proposal-id")
        assert proposal_id is not None
        proposal_url = live.url(f"/acoes/{proposal_id}/")
        expect(staff.page.locator("[data-frozen-document]")).to_have_count(1)
        recipients = staff.page.locator("[data-frozen-recipient]")
        expect(recipients).to_have_count(2)
        expect(recipients.filter(has_text=setup.portal_user.email)).to_contain_text(
            "E-mail"
        )
        expect(recipients.filter(has_text="Acesso no portal")).to_have_count(1)
        record(staff, "a-08-publication-card")

        # 4. Owner approves and executes; the client acknowledges.
        _approve_and_execute(owner, live, proposal_url)
        owner.goto(cycle_url)
        _cycle_state(owner, setup, CycleState.EXECUTION)
        owner.goto(proposal_url)
        assert (
            _post(
                owner.page,
                f"/acoes/{proposal_id}/",
                lambda: owner.page.locator('button[value="execute"]').click(),
            )
            == 302
        )
        expect(owner.page.locator("[data-decision-controls]")).to_contain_text(
            "Efeito registrado em"
        )
        record(owner, "a-09-publication-executed")
        assert dispatch_tenant(str(setup.graph.tenant.pk)) == 1
        assert len(peer.received) == 2

    with tenant_context(setup.graph.tenant.pk):
        publication = ClientPublication.objects.get(proposal_id=UUID(proposal_id))
    client.goto(live.url("/", portal=True))
    client.page.get_by_role("link", name=publication.title).click()
    expect(client.page.locator("[data-publication-body]")).to_contain_text(
        "Confira a nota do período."
    )
    record(client, "a-10-portal-publication")
    client.page.get_by_role("button", name="Confirmo o recebimento").click()
    expect(client.page.locator("[data-receipt-digest]")).to_have_attribute(
        "data-receipt-digest", publication.publication_digest
    )
    record(client, "a-11-portal-acknowledged")
    with tenant_context(setup.graph.tenant.pk):
        assert (
            ClientAcknowledgement.objects.filter(publication=publication).count() == 1
        )

    # 5. Evidence index, completion, the client's next step.
    staff.goto(live.url(f"/ciclos/{setup.cycle.pk}/evidencias/"))
    staff.page.get_by_role("button", name="Gerar índice atualizado").click()
    expect(staff.page.locator("#index-generation")).to_be_visible()
    expect(staff.page.locator("main")).to_contain_text("SHA-256")
    staff.goto(cycle_url)
    staff.page.locator("#target").select_option(CycleState.COMPLETED)
    staff.page.get_by_role("button", name="Alterar estado").click()
    completion = staff.page.locator("[data-approval-card]")
    expect(completion).to_be_visible()
    completion_id = completion.get_attribute("data-proposal-id")
    assert completion_id is not None
    completion_url = live.url(f"/acoes/{completion_id}/")
    _approve_and_execute(owner, live, completion_url)
    assert (
        _post(
            owner.page,
            f"/acoes/{completion_id}/",
            lambda: owner.page.locator('button[value="execute"]').click(),
        )
        == 302
    )
    owner.goto(cycle_url)
    expect(owner.page.locator("#cycle-summary")).to_contain_text(
        str(CycleState.COMPLETED.label)
    )
    record(owner, "a-12-cycle-concluido")
    with tenant_context(setup.graph.tenant.pk):
        assert ServiceCycle.objects.get(pk=setup.cycle.pk).state == CycleState.COMPLETED

    owner.goto(live.url(f"/ciclos/{setup.cycle.pk}/evidencias/"))
    index = owner.page.locator("main")
    expect(index).to_contain_text("emenda")
    record(owner, "a-13-evidence-index")

    client.goto(live.url("/", portal=True))
    expect(client.page.locator("main")).to_contain_text(str(CycleState.COMPLETED.label))
    expect(client.page.locator("main")).not_to_contain_text(PRIVATE)
    record(client, "a-14-portal-next-step")
    assert_policed_roles()
    staff.assert_clean_console()
    owner.assert_clean_console()
    client.assert_clean_console()
