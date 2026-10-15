"""AXR1-043: the client workspace links the competence's requests and communication.

Every import here exists at b9bba19, so this module runs on the parent head and fails
there by assertion, not by import: the parent renders one placeholder sentence in
Solicitações and in Comunicação and no typed row at all. Assertions read data
attributes and links, never prose. The typed view-model contract lives in
test_workspace_sections.py beside this module.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
from uuid import UUID

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.effects.executors import execute
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.evidence import RequestKind
from apps.cycles.models import ClientPublication, EvidenceRequest, RequestState
from apps.cycles.publications import PublicationState
from apps.cycles.services.approvals import approve, propose
from apps.cycles.services.requests import send_requests
from apps.delivery.contracts import Claim, Reply
from apps.delivery.gateway import send
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.outbox import enqueue
from apps.delivery.policy import BusinessIntent
from apps.delivery.services import claim_batch, finalize
from apps.delivery.tasks import dispatch_tenant
from tests.cycles.cycle_fixtures import PERIOD
from tests.cycles.evidence_fixtures import imported_setup
from tests.delivery.smtp import FakeSMTP
from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)

HOSTS = [".localhost", "localhost", "testserver"]
THIS_PERIOD = f"{PERIOD:%Y-%m}"
NEXT_PERIOD = "2026-10"


@dataclass(frozen=True)
class Element:
    section: str | None
    tag: str
    attrs: dict[str, str]
    owners: tuple[dict[str, str], ...]


class SectionMarkup(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[Element] = []
        self._sections: list[str | None] = []
        self._items: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "section":
            self._sections.append(values.get("id"))
            self._items = []
        section = next((name for name in reversed(self._sections) if name), None)
        self.elements.append(Element(section, tag, values, tuple(self._items)))
        if tag == "li":
            self._items.append(values)

    def handle_endtag(self, tag: str) -> None:
        if tag == "li" and self._items:
            self._items.pop()
        elif tag == "section" and self._sections:
            self._sections.pop()
            self._items = []


def markup(html: str) -> list[Element]:
    parser = SectionMarkup()
    parser.feed(html)
    parser.close()
    return parser.elements


def rows(page: list[Element], section: str, attribute: str) -> list[dict[str, str]]:
    return [
        element.attrs
        for element in page
        if element.section == section and attribute in element.attrs
    ]


def inside(page: list[Element], attribute: str, value: str) -> list[Element]:
    return [
        element
        for element in page
        if any(owner.get(attribute) == value for owner in element.owners)
    ]


def links(page: list[Element], attribute: str, value: str) -> set[str]:
    return {
        element.attrs.get("href", "")
        for element in inside(page, attribute, value)
        if element.tag == "a"
    }


def workspace_url(client_id: UUID, period: str) -> str:
    return f"{reverse('client-detail', args=[client_id])}?periodo={period}"


@contextmanager
def smtp_peer(mode: str) -> Iterator[FakeSMTP]:
    with (
        FakeSMTP(mode=mode) as peer,
        override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="127.0.0.1",
            EMAIL_PORT=peer.port,
            EMAIL_HOST_USER="",
            EMAIL_HOST_PASSWORD="",
            EMAIL_USE_TLS=False,
            EMAIL_USE_SSL=False,
            EMAIL_TIMEOUT=3,
        ),
    ):
        yield peer


@dataclass(frozen=True)
class PeriodWorld:
    setup: PortalSetup
    request: EvidenceRequest
    request_notice: OutboundMessage
    publication: ClientPublication
    publication_notice: OutboundMessage

    def url(self, period: str = THIS_PERIOD) -> str:
        return workspace_url(self.setup.graph.client.pk, period)


def _publish(setup: PortalSetup) -> ClientPublication:
    setup.cycle.refresh_from_db()
    proposal = propose(
        setup.graph.staff,
        setup.cycle.pk,
        ProposalDraft(
            ProposalInput(
                action_kind=ActionKind.CLIENT_PUBLICATION,
                step_key="t43-publication",
                recipient_ids=(setup.contact.pk,),
                subject="Guia de setembro",
                body="Confira a guia do período.",
            ),
            timezone.now() + timedelta(days=1),
        ),
        expected_version=setup.cycle.version,
    )
    proposal = approve(
        setup.graph.actor, proposal.pk, expected_version=proposal.version
    ).proposal
    execute(setup.graph.actor, proposal.pk, expected_version=proposal.version)
    return ClientPublication.objects.get(proposal=proposal)


def period_world(settings: SettingsWrapper, tmp_path: Path, slug: str) -> PeriodWorld:
    """An imported client with a sent request, a publication and its receipt.

    Real services throughout: the CSV import, send_requests, approval and
    execution, the dispatcher over a fake SMTP peer and the portal's explicit
    receipt POST. The request notice then gets a late cancellation, which the
    monotone state machine records and ignores (AXR1-044).
    """
    settings.ALLOWED_HOSTS = HOSTS
    settings.DELIVERY_PAUSED = False
    settings.MEDIA_ROOT = tmp_path
    setup = imported_setup(slug)
    tenant_id = setup.graph.tenant.pk
    with smtp_peer("accept"):
        with tenant_context(tenant_id):
            setup.cycle.refresh_from_db()
            send_requests(
                setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
            )
            publication = _publish(setup)
        assert dispatch_tenant(str(tenant_id)) == 2
    receipt = setup.portal().post(
        f"/publicacoes/{publication.pk}/",
        {"publication_digest": publication.publication_digest},
    )
    assert receipt.status_code in {200, 302}
    with tenant_context(tenant_id):
        request_notice = OutboundMessage.objects.get(kind="request_notice")
        publication_notice = OutboundMessage.objects.get(
            proposal_id=publication.proposal_id
        )
        request = EvidenceRequest.objects.get(pk=setup.request.pk)
    assert request.state == RequestState.REQUESTED
    assert request_notice.state == MessageState.ACCEPTED
    assert publication_notice.state == MessageState.ACCEPTED
    assert request_notice.lease_owner is not None
    late = Claim(
        tenant_id, request_notice.pk, request_notice.fence, request_notice.lease_owner
    )
    assert not finalize(
        late, Reply(MessageState.CANCELLED, "lease_expired_before_dispatch")
    )
    with tenant_context(tenant_id):
        request_notice.refresh_from_db()
    assert request_notice.state == MessageState.ACCEPTED
    return PeriodWorld(setup, request, request_notice, publication, publication_notice)


def deliver_notice(
    world: PeriodWorld, mode: str, *, occurrence: int
) -> OutboundMessage:
    graph = world.setup.graph
    cycle = world.setup.cycle
    with smtp_peer(mode):
        with tenant_context(graph.tenant.pk):
            graph.client.refresh_from_db()
            cycle.refresh_from_db()
            intent = BusinessIntent(
                graph.engagement.pk,
                cycle.playbook_id,
                cycle.period_start,
                cycle.period_end,
                cycle.amendment,
                "notice",
                "email_notice",
            )
            message = enqueue(
                graph.actor,
                graph.client.pk,
                world.setup.contact.pk,
                intent=replace(intent, occurrence=occurrence),
                kind="notice",
                expected_version=graph.client.version,
                cycle_id=cycle.pk,
            )
        [claim] = claim_batch(graph.tenant.pk)
        assert finalize(claim, send(claim))
    with tenant_context(graph.tenant.pk):
        message.refresh_from_db()
    return message


def history(page: list[Element], message: OutboundMessage) -> list[tuple[str, str]]:
    return [
        (element.attrs["data-observed-state"], element.attrs["data-observation-effect"])
        for element in inside(page, "data-message-id", str(message.pk))
        if "data-observation-effect" in element.attrs
    ]


def test_workspace_links_the_period_request_and_its_communication(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    """Red witness: at b9bba19 neither section renders a single typed row."""
    world = period_world(settings, tmp_path, "t43-linked")

    response = world.setup.firm().get(world.url())

    assert response.status_code == 200
    page = markup(response.content.decode())
    requests = [
        (
            row["data-request-id"],
            row["data-request-state"],
            row["data-request-kind"],
            row["data-client-status"],
        )
        for row in rows(page, "solicitacoes", "data-request-id")
    ]
    assert requests == [
        (
            str(world.request.pk),
            RequestState.REQUESTED,
            RequestKind.DOCUMENTO,
            "atrasada",
        )
    ]
    cycle_href = reverse("cycle-detail", args=[world.setup.cycle.pk])
    assert f"{cycle_href}#solicitacao-{world.request.pk}" in links(
        page, "data-request-id", str(world.request.pk)
    )
    messages = {
        row["data-message-id"]: (row["data-message-kind"], row["data-message-state"])
        for row in rows(page, "comunicacao", "data-message-id")
    }
    assert messages == {
        str(world.request_notice.pk): ("request_notice", MessageState.ACCEPTED),
        str(world.publication_notice.pk): (
            "approved_publication_notice",
            MessageState.ACCEPTED,
        ),
    }
    queue = reverse("delivery-queue")
    for message_id in messages:
        assert f"{queue}?mensagem={message_id}" in links(
            page, "data-message-id", message_id
        )
    *earlier, last = history(page, world.request_notice)
    assert last == (MessageState.CANCELLED, "ignored_late")
    assert (MessageState.ACCEPTED, "applied") in earlier
    assert {effect for _, effect in earlier} == {"applied"}
    publications = [
        (
            row["data-publication-id"],
            row["data-publication-state"],
            row["data-acknowledged"],
        )
        for row in rows(page, "comunicacao", "data-publication-id")
    ]
    assert publications == [
        (str(world.publication.pk), PublicationState.PUBLISHED, "true")
    ]
    receipts = [
        element.attrs["data-acknowledgement"]
        for element in inside(page, "data-publication-id", str(world.publication.pk))
        if "data-acknowledgement" in element.attrs
    ]
    assert receipts == [world.publication.publication_digest]
    assert reverse("approval-card", args=[world.publication.proposal_id]) in links(
        page, "data-publication-id", str(world.publication.pk)
    )
    assert rows(page, "solicitacoes", "data-ui-state") == []
    assert rows(page, "comunicacao", "data-ui-state") == []


def test_an_empty_period_is_typed_empty_and_the_selector_moves_every_row(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    world = period_world(settings, tmp_path, "t43-periods")
    firm = world.setup.firm()

    later = markup(firm.get(world.url(NEXT_PERIOD)).content.decode())
    back = markup(firm.get(world.url(THIS_PERIOD)).content.decode())

    for attribute in ("data-request-id", "data-message-id", "data-publication-id"):
        assert rows(later, "solicitacoes", attribute) == []
        assert rows(later, "comunicacao", attribute) == []
    assert [
        row["data-ui-state"] for row in rows(later, "solicitacoes", "data-ui-state")
    ] == ["empty"]
    assert [
        row["data-ui-state"] for row in rows(later, "comunicacao", "data-ui-state")
    ] == ["empty", "empty"]
    selected = [
        element.attrs["value"]
        for element in later
        if element.tag == "input" and element.attrs.get("type") == "month"
    ]
    assert selected == [NEXT_PERIOD]
    assert [
        row["data-request-id"] for row in rows(back, "solicitacoes", "data-request-id")
    ] == [str(world.request.pk)]
    assert {
        row["data-message-id"] for row in rows(back, "comunicacao", "data-message-id")
    } == {str(world.request_notice.pk), str(world.publication_notice.pk)}
    assert len(rows(back, "comunicacao", "data-publication-id")) == 1


def test_failed_uncertain_and_accepted_messages_keep_their_own_outcome(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    world = period_world(settings, tmp_path, "t43-outcomes")
    rejected = deliver_notice(world, "reject", occurrence=1)
    uncertain = deliver_notice(world, "disconnect", occurrence=2)
    assert rejected.state == MessageState.REJECTED
    assert uncertain.state == MessageState.UNKNOWN

    page = markup(world.setup.firm().get(world.url()).content.decode())

    states = {
        row["data-message-id"]: row["data-message-state"]
        for row in rows(page, "comunicacao", "data-message-id")
    }
    assert states == {
        str(world.request_notice.pk): MessageState.ACCEPTED,
        str(world.publication_notice.pk): MessageState.ACCEPTED,
        str(rejected.pk): MessageState.REJECTED,
        str(uncertain.pk): MessageState.UNKNOWN,
    }
    for message in (rejected, uncertain):
        observed = history(page, message)
        assert observed[-1] == (message.state, "applied")
        assert {effect for _, effect in observed} == {"applied"}
    [publication] = rows(page, "comunicacao", "data-publication-id")
    assert publication["data-acknowledged"] == "true"


def test_portfolio_scope_hides_a_forbidden_client_and_never_mixes_rows(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    world = period_world(settings, tmp_path, "t43-scope")
    setup = world.setup
    staff = setup.firm(setup.graph.staff)
    foreign_url = workspace_url(setup.other_client.pk, THIS_PERIOD)

    denied = staff.get(foreign_url)
    assigned = markup(staff.get(world.url()).content.decode())
    foreign = markup(setup.firm().get(foreign_url).content.decode())

    assert denied.status_code == 404
    assert [
        row["data-request-id"]
        for row in rows(assigned, "solicitacoes", "data-request-id")
    ] == [str(world.request.pk)]
    assert {
        row["data-message-id"]
        for row in rows(assigned, "comunicacao", "data-message-id")
    } == {str(world.request_notice.pk), str(world.publication_notice.pk)}
    assert [
        row["data-request-id"]
        for row in rows(foreign, "solicitacoes", "data-request-id")
    ] == [str(setup.other_request.pk)]
    assert rows(foreign, "comunicacao", "data-message-id") == []
    assert rows(foreign, "comunicacao", "data-publication-id") == []
