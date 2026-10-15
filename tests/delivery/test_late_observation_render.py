"""AXR1-044: an ignored late observation never reads as the current delivery state.

Every import here exists at 4aed5df, so this module runs on the parent head and fails
there by assertion, not by import: the parent projects a late cancellation as a bare
state word after the acceptance, and its history markup carries no observation type.
The typed view-model contract lives in the projection module beside this one.
"""

from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from uuid import UUID

import pytest
from django.test import Client, override_settings
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.effects.executors import execute
from apps.cycles.models import ClientPublication
from apps.delivery.contracts import Claim, Reply
from apps.delivery.gateway import send
from apps.delivery.models import DeliveryAttempt, MessageState, OutboundMessage
from apps.delivery.services import claim_batch, finalize
from apps.delivery.tasks import dispatch_tenant
from tests.browser.harness import mfa_session_cookie
from tests.cycles.cycle_fixtures import PERIOD
from tests.cycles.publication_fixtures import PublicationGraph, publication_graph
from tests.delivery.conftest import DeliverySetup
from tests.delivery.smtp import FakeSMTP
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

HOSTS = [".localhost", "localhost", "testserver"]


class ListItems(HTMLParser):
    """Collect the attributes of every rendered list item, in document order."""

    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "li":
            self.items.append(dict(attrs))


def list_items(html: str) -> list[dict[str, str | None]]:
    parser = ListItems()
    parser.feed(html)
    return parser.items


def accepted_claim(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> tuple[OutboundMessage, Claim]:
    smtp_server("accept")
    message = delivery.queue()
    claim = claim_batch(delivery.graph.tenant.pk)[0]
    assert finalize(claim, send(claim))
    assert delivery.reload(message).state == MessageState.ACCEPTED
    return message, claim


def late_claim(tenant_id: UUID, message: OutboundMessage) -> Claim:
    """Rebuild the still-current claim a worker's late reply would carry."""
    assert message.lease_owner is not None
    return Claim(tenant_id, message.pk, message.fence, message.lease_owner)


def firm_web(delivery: DeliverySetup) -> Client:
    actor = delivery.graph.actor
    enrol_totp(actor)
    host = f"{delivery.graph.tenant.slug}.localhost"
    web = Client(SERVER_NAME=host)
    cookie = mfa_session_cookie(actor, f"http://{host}/")
    assert "name" in cookie
    assert "value" in cookie
    web.cookies[cookie["name"]] = cookie["value"]
    return web


def acknowledged_publication(
    settings: SettingsWrapper, tmp_path: Path, slug: str
) -> tuple[PublicationGraph, OutboundMessage]:
    """Publish, accept over a real fake SMTP peer, then record the client's receipt."""
    settings.ALLOWED_HOSTS = HOSTS
    settings.DELIVERY_PAUSED = False
    settings.MEDIA_ROOT = tmp_path
    with (
        FakeSMTP(mode="accept") as peer,
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
        setup = publication_graph(slug)
        with tenant_context(setup.graph.tenant.pk):
            execute(setup.graph.actor, setup.proposal.pk, expected_version=2)
            publication = ClientPublication.objects.get(proposal=setup.proposal)
        assert dispatch_tenant(str(setup.graph.tenant.pk)) == 1
    receipt = setup.session().post(
        f"/publicacoes/{publication.pk}/",
        {"publication_digest": publication.publication_digest},
    )
    assert receipt.status_code in {200, 302}
    with tenant_context(setup.graph.tenant.pk):
        message = OutboundMessage.objects.get(proposal_id=setup.proposal.pk)
    assert message.state == MessageState.ACCEPTED
    return setup, message


def test_workspace_history_never_reads_a_late_cancellation_as_current_state(
    delivery: DeliverySetup,
    smtp_server: Callable[[str], FakeSMTP],
    settings: SettingsWrapper,
) -> None:
    """Red witness: at 4aed5df the newest unflagged entry is the late cancellation."""
    settings.ALLOWED_HOSTS = HOSTS
    message, claim = accepted_claim(delivery, smtp_server)
    late = Reply(MessageState.CANCELLED, "lease_expired_before_dispatch")
    assert not finalize(claim, late)
    assert delivery.reload(message).state == MessageState.ACCEPTED

    response = firm_web(delivery).get(
        f"/clientes/{delivery.graph.client.pk}/?periodo={PERIOD:%Y-%m}"
    )

    assert response.status_code == 200
    with tenant_context(delivery.graph.tenant.pk):
        attempt_ids = set(
            DeliveryAttempt.objects.filter(message=message).values_list("pk", flat=True)
        )
    entries = [
        entry for entry in response.context["timeline"] if entry.id in attempt_ids
    ]
    flags = [bool(getattr(entry, "ignored", False)) for entry in entries]
    current = [
        entry for entry, ignored in zip(entries, flags, strict=True) if not ignored
    ]
    assert current[0].summary == MessageState.ACCEPTED.label
    assert flags.count(True) == 1
    rendered = [
        item
        for item in list_items(response.content.decode())
        if item.get("data-observation") == "ignored"
    ]
    assert len(rendered) == 1


def test_queue_keeps_the_receipt_and_types_a_late_failure_after_acceptance(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    setup, message = acknowledged_publication(settings, tmp_path, "late-receipt")
    late = late_claim(setup.graph.tenant.pk, message)
    assert not finalize(late, Reply(MessageState.REJECTED))

    response = setup.session(portal=False).get(f"/comunicacao/?mensagem={message.pk}")

    assert response.status_code == 200
    html = response.content.decode()
    assert f'data-state="{MessageState.ACCEPTED}"' in html
    items = list_items(html)
    history = [
        (item.get("data-observed-state"), item.get("data-observation-effect"))
        for item in items
        if "data-observation-effect" in item
    ]
    assert history, "the send history carries no observation type"
    assert history[-1] == (MessageState.REJECTED, "ignored_late")
    assert (MessageState.ACCEPTED, "applied") in history[:-1]
    assert {effect for _, effect in history[:-1]} == {"applied"}
    assert sum("data-acknowledgement" in item for item in items) == 1
