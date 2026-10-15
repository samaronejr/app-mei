"""Owned-opener destinations identify real metadata, not merely a successful page."""

from datetime import timedelta
from html.parser import HTMLParser
from typing import override
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.clients.models import (
    ClientAssignment,
    ClientContact,
    Engagement,
    TaxProfile,
    TaxRegime,
)
from apps.core.tenancy import tenant_context
from apps.cycles.models import EvidenceRequest, RequestState
from apps.cycles.openers import evaluate_tenant_exceptions
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.outbox import enqueue
from apps.delivery.policy import BusinessIntent
from tests.cycles.cycle_fixtures import cycle_setup
from tests.cycles.test_exceptions import TODAY, _cycle, _firm
from tests.portal.request_fixtures import portal_setup

pytestmark = pytest.mark.django_db(transaction=True)
EXPIRY = TODAY + timedelta(days=20)
REQUEST_TITLE = "SUBJECT-REQUEST-940F"


class Target(HTMLParser):
    """Read one element's attributes, text and dates, including nested content."""

    def __init__(self, identifier: str) -> None:
        super().__init__(convert_charrefs=True)
        self.identifier = identifier
        self.tag = ""
        self.depth = 0
        self.attributes: dict[str, str | None] = {}
        self.text: list[str] = []
        self.dates: list[str | None] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if not self.tag and attributes.get("id") == self.identifier:
            self.tag = tag
            self.depth = 1
            self.attributes = attributes
        elif self.depth and tag == self.tag:
            self.depth += 1
        if self.depth and tag == "time":
            self.dates.append(attributes.get("datetime"))

    @override
    def handle_endtag(self, tag: str) -> None:
        if self.depth and tag == self.tag:
            self.depth -= 1

    @override
    def handle_data(self, data: str) -> None:
        if self.depth:
            self.text.append(data)


@pytest.mark.parametrize("surface", ["cycle", "workspace"])
@pytest.mark.parametrize(
    "reason",
    [
        "solicitacao_atrasada",
        "certificado_vencendo",
        "perfil_fiscal_desconhecido",
        "responsavel_nao_atribuido",
    ],
)
def test_owned_opener_links_reach_exact_current_metadata(
    surface: str, reason: str
) -> None:
    setup = cycle_setup("subject-evidence")
    assert setup.tenant.features == ["cycles_r1"]
    with tenant_context(setup.tenant.pk):
        cycle = _cycle(setup)
        request = EvidenceRequest.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            cycle=cycle,
            title=REQUEST_TITLE,
            due_on=TODAY - timedelta(days=1),
            state=RequestState.REQUESTED,
            requested_at=timezone.now(),
            created_by=setup.actor,
        )
        setup.client.certificate_expires_on = EXPIRY
        setup.client.save(update_fields=["certificate_expires_on"])
        profile = TaxProfile.objects.get(client=setup.client)
        TaxProfile.objects.filter(pk=profile.pk).update(regime=TaxRegime.UNKNOWN)
        Engagement.objects.filter(pk=setup.engagement.pk).update(responsible=None)
        evaluate_tenant_exceptions(setup.tenant, today=TODAY)
        # Destinations must read current metadata, not a frozen exception sentence.
        TaxProfile.objects.filter(pk=profile.pk).update(regime="mei")
        Engagement.objects.filter(pk=setup.engagement.pk).update(
            responsible=setup.actor
        )
    browser = _firm(setup)
    path = (
        f"/ciclos/{cycle.pk}/"
        if surface == "cycle"
        else f"/clientes/{setup.client.pk}/?periodo=2026-09"
    )
    response = browser.get(path)
    assert response.status_code == 200
    rows = {row["exception"].reason_code: row for row in response.context["exceptions"]}
    assert set(rows) == {
        "solicitacao_atrasada",
        "certificado_vencendo",
        "perfil_fiscal_desconhecido",
        "responsavel_nao_atribuido",
    }
    (destination,) = rows[reason]["evidence"]
    targets = {
        "solicitacao_atrasada": f"solicitacao-{request.pk}",
        "certificado_vencendo": "certificado",
        "perfil_fiscal_desconhecido": "perfil-tributario",
        "responsavel_nao_atribuido": f"servico-{setup.engagement.pk}",
    }
    identifier = targets[reason]
    assert urlsplit(destination.url).fragment == identifier
    page = browser.get(destination.url)
    assert page.status_code == 200
    target = Target(identifier)
    target.feed(page.content.decode())
    assert target.attributes.get("id") == identifier
    assert "".join(target.text).strip()
    if reason == "solicitacao_atrasada":
        assert REQUEST_TITLE in "".join(target.text)
        assert target.attributes["data-request-id"] == str(request.pk)
    elif reason == "certificado_vencendo":
        assert target.dates == [EXPIRY.isoformat()]
        assert target.attributes["data-client-id"] == str(setup.client.pk)
    elif reason == "perfil_fiscal_desconhecido":
        assert target.attributes["data-profile-id"] == str(profile.pk)
        assert target.attributes["data-regime"] == "mei"
        assert target.attributes["data-period"] == "2026-09"
    else:
        assert target.attributes["data-engagement-id"] == str(setup.engagement.pk)
        assert target.attributes["data-service-kind"] == "mei_mensal"
        assert setup.actor.email in "".join(target.text)


def test_empty_selected_message_does_not_fall_back_to_a_populated_queue() -> None:
    world = portal_setup("empty-message")
    with tenant_context(world.graph.tenant.pk):
        message = OutboundMessage.objects.get(client=world.graph.client)
    browser = world.firm()
    positive = browser.get(f"/comunicacao/?mensagem={message.pk}")
    assert positive.status_code == 200
    assert [row.pk for row in positive.context["page"]] == [message.pk]
    assert browser.get("/comunicacao/?mensagem=").status_code == 404


def test_selected_message_refuses_malformed_foreign_and_unassigned_identities() -> None:
    world = portal_setup("scoped-message")
    foreign = portal_setup("foreign-message")
    with tenant_context(world.graph.tenant.pk):
        own = OutboundMessage.objects.get(client=world.graph.client)
        contact = ClientContact.objects.create(
            tenant=world.graph.tenant,
            client=world.other_client,
            name="Other scoped contact",
            email="other@scope.test",
        )
        other_cycle = world.other_request.cycle
        sibling = enqueue(
            world.graph.actor,
            world.other_client.pk,
            contact.pk,
            intent=BusinessIntent(
                other_cycle.engagement_id,
                other_cycle.playbook_id,
                other_cycle.period_start,
                other_cycle.period_end,
                other_cycle.amendment,
                "sibling-selection",
                "notice",
            ),
            expected_version=world.other_client.version,
            cycle_id=other_cycle.pk,
            idempotency_key="sibling-selection",
        )
        sibling.subject = "OTHER-CLIENT-MESSAGE"
        sibling.state = MessageState.UNKNOWN
        sibling.save(update_fields=["subject", "state"])
        ClientAssignment.objects.create(
            tenant=world.graph.tenant,
            client=world.graph.client,
            user=world.graph.staff,
        )
    with tenant_context(foreign.graph.tenant.pk):
        foreign_message = OutboundMessage.objects.get(client=foreign.graph.client)
    browser = world.firm(world.graph.staff)
    positive = browser.get(f"/comunicacao/?mensagem={own.pk}")
    assert positive.status_code == 200
    assert [row.pk for row in positive.context["page"]] == [own.pk]
    for identifier in (
        "bad-id",
        str(uuid4()),
        str(sibling.pk),
        str(foreign_message.pk),
    ):
        refused = browser.get(f"/comunicacao/?mensagem={identifier}")
        assert refused.status_code == 404
        assert "OTHER-CLIENT-MESSAGE" not in refused.content.decode()
    assert world.firm().get(f"/comunicacao/?mensagem={sibling.pk}").status_code == 200
