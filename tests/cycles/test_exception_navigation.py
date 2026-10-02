"""Real object navigation and durable selected-exception HTTP state."""

from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import override
from urllib.parse import urlsplit

import pytest
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.openers import REQUEST_OVERDUE
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.services import report_problem
from tests.cycles.document_fixtures import reviewed
from tests.cycles.test_exception_subjects import Target
from tests.cycles.test_exceptions import _open
from tests.portal.request_fixtures import PortalSetup, portal_setup

pytestmark = pytest.mark.django_db(transaction=True)


class ExceptionLinks(HTMLParser):
    """Collect links only inside the staff exception section, not document lists."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.section_depth: int = 0
        self.hrefs: list[str] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "section" and (
            self.section_depth or attributes.get("id") == "pendencias"
        ):
            self.section_depth += 1
        href = attributes.get("href")
        if self.section_depth and tag == "a" and href:
            self.hrefs.append(href)

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag == "section" and self.section_depth:
            self.section_depth -= 1


def _surface(world: PortalSetup, surface: str) -> str:
    if surface == "cycle":
        return f"/ciclos/{world.cycle.pk}/"
    return f"/clientes/{world.graph.client.pk}/?periodo=2026-09"


@pytest.mark.parametrize("surface", ["cycle", "workspace"])
def test_document_evidence_reaches_the_actual_scoped_link(
    settings: SettingsWrapper, tmp_path: Path, surface: str
) -> None:
    """An exception must navigate to its EvidenceLink, not its Document UUID."""
    settings.MEDIA_ROOT = tmp_path
    world = portal_setup("t14-fix-doc")
    intake, link = reviewed(world, "nfe_outra_empresa.xml")
    assert link.pk != intake.document.pk
    browser = world.firm()
    response = browser.get(_surface(world, surface))
    assert response.status_code == 200
    links = ExceptionLinks()
    links.feed(response.content.decode())
    expected = f"/documentos/{link.pk}/revisar/"
    destinations = [href for href in links.hrefs if urlsplit(href).path == expected]
    assert destinations, links.hrefs
    destination = browser.get(destinations[0])
    assert destination.status_code == 200
    assert destination.context["link"].pk == link.pk
    assert destination.context["document"].pk == intake.document.pk
    assert destination.context["link"].client_id == world.graph.client.pk
    assert world.firm(world.graph.staff).get(destinations[0]).status_code == 404


@pytest.mark.parametrize("surface", ["cycle", "workspace"])
def test_delivery_evidence_selects_its_actual_message_past_page_one(
    surface: str,
) -> None:
    """An unfiltered queue or ignored fragment cannot pass the destination check."""
    world = portal_setup("t14-fix-delivery")
    with tenant_context(world.graph.tenant.pk):
        message = OutboundMessage.objects.get(
            client=world.graph.client, kind="request_notice"
        )
        message.state = MessageState.UNKNOWN
        message.created_at = timezone.now() - timedelta(days=1)
        message.save(update_fields=["state", "created_at"])
        report_problem(message)
        # Real persisted peer rows force the target past the existing 30-row page.
        OutboundMessage.objects.bulk_create(
            OutboundMessage(
                tenant=world.graph.tenant,
                client=world.graph.client,
                contact=world.contact,
                cycle=world.cycle,
                created_by=world.graph.actor,
                kind="request_notice",
                idempotency_key=f"t14-evidence-peer-{index}",
                business_intent_key=f"t14-evidence-peer-{index}",
                content_digest="1" * 64,
                recipients_json=[world.contact.email],
                subject=f"Other synthetic message {index}",
                body_ref="delivery/email/request_notice.txt",
                portal_url=f"http://{world.portal_host}/",
                state=MessageState.UNKNOWN,
            )
            for index in range(31)
        )
    browser = world.firm()
    response = browser.get(_surface(world, surface))
    assert response.status_code == 200
    links = ExceptionLinks()
    links.feed(response.content.decode())
    destinations = [
        href for href in links.hrefs if urlsplit(href).path == "/comunicacao/"
    ]
    assert destinations, links.hrefs
    destination = browser.get(destinations[0])
    assert destination.status_code == 200
    assert [row.pk for row in destination.context["page"]] == [message.pk]
    assert world.firm(world.graph.staff).get(destinations[0]).status_code == 404


@pytest.mark.parametrize("origin", ["ciclo", "cliente"])
def test_selected_drawer_survives_actions_conflicts_and_resolution(origin: str) -> None:
    world = portal_setup("selected-drawer")
    with tenant_context(world.graph.tenant.pk):
        problem = _open(world.graph, world.cycle, REQUEST_OVERDUE, world.request.pk)
    browser = world.firm()
    base = (
        f"/ciclos/{world.cycle.pk}/?"
        if origin == "ciclo"
        else f"/clientes/{world.graph.client.pk}/?periodo=2026-09&"
    )
    selected_url = f"{base}pendencia={problem.pk}#pendencias"
    response = browser.get(selected_url)
    assert response.status_code == 200
    selected = Target(f"exception-{problem.pk}")
    selected.feed(response.content.decode())
    assert "open" in selected.attributes
    action = f"/ciclos/pendencias/{problem.pk}/?origem={origin}"
    posted = browser.post(
        action, {"action": "em_tratamento", "expected_version": 1, "origin": origin}
    )
    assert posted.status_code == 302
    assert posted["Location"] == selected_url
    stale = browser.post(
        action, {"action": "em_tratamento", "expected_version": 1, "origin": origin}
    )
    assert stale.status_code == 409
    selected = Target(f"exception-{problem.pk}")
    selected.feed(stale.content.decode())
    assert "open" in selected.attributes
    error = Target(f"exception-error-{problem.pk}")
    error.feed(stale.content.decode())
    assert "".join(error.text).strip()
    if origin == "cliente":
        assert stale.context["period"].isoformat() == "2026-09-01"
    reloaded = browser.get(action)
    assert reloaded.status_code == 302
    assert reloaded["Location"] == selected_url
    resolved = browser.post(
        action,
        {
            "action": "resolvida",
            "expected_version": 2,
            "origin": origin,
            "note": "Synthetic resolution record.",
        },
    )
    assert resolved.status_code == 302
    assert resolved["Location"] == selected_url
    final = browser.get(selected_url)
    assert final.status_code == 200
    selected = Target(f"exception-{problem.pk}")
    selected.feed(final.content.decode())
    assert "open" in selected.attributes
    state = Target(f"exception-state-{problem.pk}")
    state.feed(final.content.decode())
    assert state.attributes["data-state"] == "resolvida"
