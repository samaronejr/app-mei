"""Portal invitations on the outbox (T11): minted at dispatch, cancelled when dead.

* no raw token in any outbox row, attempt, or captured log line;
* every dispatch attempt mints a NEW token, and the previous link answers 410;
* a revoked or accepted invitation, or an inactive contact, cancels the queued send;
* both issuing paths (setup-page contact invite, client-detail invite) enqueue inside
  the request transaction and send nothing there; a rollback leaves neither row.
"""

import email
import logging
import re
from http import HTTPStatus
from typing import Any

import pytest
from django.core import mail
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.invites import revoke_invite
from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.delivery import outbox
from apps.delivery.models import DeliveryAttempt, MessageState, OutboundMessage
from apps.delivery.tasks import dispatch_outbox
from apps.tenants.models import Invite, TenantRole
from tests.browser.harness import mfa_session_cookie
from tests.cycles.cycle_fixtures import CycleSetup, cycle_setup
from tests.delivery.smtp import FakeSMTP
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

INVITED = "maria@padaria.test"
ACCEPT = re.compile(rb"/convites/aceitar/([A-Za-z0-9_\-]+)/")


def _smtp(port: int) -> override_settings:
    return override_settings(
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
        EMAIL_HOST="127.0.0.1",
        EMAIL_PORT=port,
        EMAIL_HOST_USER="",
        EMAIL_HOST_PASSWORD="",
        EMAIL_USE_TLS=False,
        EMAIL_USE_SSL=False,
        EMAIL_TIMEOUT=3,
    )


@pytest.fixture
def graph(settings: SettingsWrapper) -> CycleSetup:
    settings.DELIVERY_PAUSED = False
    setup = cycle_setup("invite-outbox")
    setup.tenant.features = ["cycles_r1", "import_r1"]
    setup.tenant.save(update_fields=["features"])
    enrol_totp(setup.actor)
    return setup


def _firm(graph: CycleSetup, **extra: Any) -> Client:  # noqa: ANN401
    host = f"{graph.tenant.slug}.localhost"
    browser = Client(SERVER_NAME=host, **extra)
    cookie = mfa_session_cookie(graph.actor, f"http://{host}/")
    browser.cookies[cookie["name"]] = cookie["value"]
    return browser


def _contact(graph: CycleSetup) -> ClientContact:
    with tenant_context(graph.tenant.pk):
        return ClientContact.objects.create(
            tenant=graph.tenant, client=graph.client, name="Dona Maria", email=INVITED
        )


def _invite_contact(graph: CycleSetup, contact: ClientContact) -> None:
    response = _firm(graph).post(
        reverse("client-contact-invite", args=[graph.client.pk, contact.pk]),
        {"convite-role": TenantRole.CLIENT_OWNER, "convite-version": contact.version},
    )
    assert response.status_code == HTTPStatus.FOUND
    assert "convite=na-fila" in response["Location"]
    page = _firm(graph).get(response["Location"])
    assert "Convite na fila de envio" in page.content.decode()


def _messages(graph: CycleSetup) -> list[OutboundMessage]:
    with tenant_context(graph.tenant.pk):
        return list(OutboundMessage.objects.filter(kind="portal_invite"))


def _tokens(server: FakeSMTP) -> list[str]:
    tokens = []
    for raw in server.received:
        parsed = email.message_from_bytes(raw)
        body = parsed.get_payload(decode=True)
        assert isinstance(body, bytes)
        match = ACCEPT.search(body)
        assert match is not None, body
        tokens.append(match.group(1).decode())
    return tokens


def _accept_status(graph: CycleSetup, token: str) -> int:
    portal = Client(SERVER_NAME=f"{graph.tenant.slug}-portal.localhost")
    return portal.get(f"/convites/aceitar/{token}/").status_code


def _stored_text(graph: CycleSetup) -> str:
    """Every value of every outbox row and attempt, as one string."""
    with tenant_context(graph.tenant.pk):
        rows = [
            *OutboundMessage.objects.values(),
            *DeliveryAttempt.objects.values(),
        ]
    return repr(rows)


def test_contact_invite_is_queued_minted_at_dispatch_and_never_stored(
    graph: CycleSetup, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    contact = _contact(graph)
    mail.outbox.clear()
    _invite_contact(graph, contact)
    assert mail.outbox == []
    [message] = _messages(graph)
    assert message.state == MessageState.QUEUED
    assert message.invite_id is not None
    with FakeSMTP() as server, _smtp(server.port):
        dispatch_outbox()
    [token] = _tokens(server)
    [message] = _messages(graph)
    assert message.state == MessageState.ACCEPTED
    stored = _stored_text(graph)
    assert token not in stored
    # Every record our process logged; the fake SMTP PEER's own DATA trace
    # (aiosmtpd's `mail.log`) is the recipient's server receiving the e-mail, which is
    # where the token is supposed to go.
    ours = [
        f"{record.getMessage()} {record.__dict__}"
        for record in caplog.records
        if not record.name.startswith("mail.log")
    ]
    assert ours, "nothing was captured, so the absence below proves nothing"
    assert not [line for line in ours if token in line]
    assert message.invite_id is not None
    # PLATFORM_QUERY_OK: test assertion on the invitation the message names.
    invite = Invite.objects.get(pk=message.invite_id)
    assert token not in repr(Invite.objects.filter(pk=invite.pk).values().get())
    assert _accept_status(graph, token) == HTTPStatus.OK


def test_a_retry_mints_a_new_token_and_the_old_link_answers_410(
    graph: CycleSetup,
) -> None:
    contact = _contact(graph)
    _invite_contact(graph, contact)
    with FakeSMTP(mode="temporary") as down, _smtp(down.port):
        dispatch_outbox()
    [first] = _tokens(down)
    [message] = _messages(graph)
    assert message.state == MessageState.TEMPORARY
    with tenant_context(graph.tenant.pk):
        OutboundMessage.objects.filter(pk=message.pk).update(
            next_attempt_at=timezone.now()
        )
    with FakeSMTP() as up, _smtp(up.port):
        dispatch_outbox()
    [second] = _tokens(up)
    assert first != second
    assert _accept_status(graph, first) == HTTPStatus.GONE
    assert _accept_status(graph, second) == HTTPStatus.OK
    assert first not in _stored_text(graph)
    assert second not in _stored_text(graph)


def test_a_revoked_invite_cancels_its_queued_message(graph: CycleSetup) -> None:
    contact = _contact(graph)
    _invite_contact(graph, contact)
    [message] = _messages(graph)
    assert message.invite_id is not None
    # PLATFORM_QUERY_OK: the invitation the queued message names.
    invite = Invite.objects.get(pk=message.invite_id)
    revoke_invite(actor=graph.actor, invite=invite)
    with FakeSMTP() as server, _smtp(server.port):
        dispatch_outbox()
    [message] = _messages(graph)
    assert message.state == MessageState.CANCELLED
    assert message.last_error_code == "invite_revoked"
    assert server.received == []


def test_an_accepted_invite_or_inactive_contact_cancels(graph: CycleSetup) -> None:
    contact = _contact(graph)
    _invite_contact(graph, contact)
    [message] = _messages(graph)
    assert message.invite_id is not None
    # PLATFORM_QUERY_OK: the invitation the queued message names.
    Invite.objects.filter(pk=message.invite_id).update(accepted_at=timezone.now())
    with FakeSMTP() as server, _smtp(server.port):
        dispatch_outbox()
    [message] = _messages(graph)
    assert (message.state, message.last_error_code) == (
        MessageState.CANCELLED,
        "invite_accepted",
    )
    assert server.received == []


def test_an_inactive_contact_cancels_the_invitation(graph: CycleSetup) -> None:
    contact = _contact(graph)
    _invite_contact(graph, contact)
    with tenant_context(graph.tenant.pk):
        ClientContact.objects.filter(pk=contact.pk).update(active=False)
    with FakeSMTP() as server, _smtp(server.port):
        dispatch_outbox()
    [message] = _messages(graph)
    assert (message.state, message.last_error_code) == (
        MessageState.CANCELLED,
        "recipient_not_allowed",
    )
    assert server.received == []


def _post_detail_invite(graph: CycleSetup, browser: Client) -> int:
    return browser.post(
        reverse("portal-invite-issue", args=[graph.client.pk]),
        {"email": INVITED, "role": TenantRole.CLIENT_OWNER},
    ).status_code


def test_detail_page_invite_is_queued_not_sent_in_the_request(
    graph: CycleSetup,
) -> None:
    mail.outbox.clear()
    assert _post_detail_invite(graph, _firm(graph)) == HTTPStatus.FOUND
    assert mail.outbox == []
    [message] = _messages(graph)
    with tenant_context(graph.tenant.pk):
        contact = ClientContact.objects.get(pk=message.contact_id)
    assert contact.email == INVITED
    assert contact.client_id == graph.client.pk
    with FakeSMTP() as server, _smtp(server.port):
        dispatch_outbox()
    [token] = _tokens(server)
    assert _accept_status(graph, token) == HTTPStatus.OK


def test_detail_page_invite_rolls_back_with_its_message(
    graph: CycleSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after enqueue (e.g. racing an import rollback) leaves no row and
    sends nothing: the invitation and its message share the request transaction."""
    real = outbox.enqueue_portal_invite

    def enqueue_then_fail(*args: Any, **kwargs: Any) -> OutboundMessage:  # noqa: ANN401
        real(*args, **kwargs)
        msg = "simulated failure after enqueue"
        raise RuntimeError(msg)

    monkeypatch.setattr(outbox, "enqueue_portal_invite", enqueue_then_fail)
    mail.outbox.clear()
    status = _post_detail_invite(graph, _firm(graph, raise_request_exception=False))
    assert status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert mail.outbox == []
    assert _messages(graph) == []
    # PLATFORM_QUERY_OK: no invitation for this firm survived the rollback.
    assert not Invite.objects.filter(tenant=graph.tenant).exists()


def test_unflagged_firm_keeps_the_synchronous_invitation(graph: CycleSetup) -> None:
    graph.tenant.features = ["import_r1"]
    graph.tenant.save(update_fields=["features"])
    mail.outbox.clear()
    assert _post_detail_invite(graph, _firm(graph)) == HTTPStatus.FOUND
    assert [message.to for message in mail.outbox] == [[INVITED]]
    assert _messages(graph) == []
