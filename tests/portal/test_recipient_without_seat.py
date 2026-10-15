"""Send preview: seat -> portal link; no seat + live invite -> invite link embedded;
neither -> ineligible, flagged in the preview and never messaged."""

import email
import re
from datetime import timedelta
from http import HTTPStatus

import pytest
from django.test import override_settings
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.invites import issue_client_invite
from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.cycles.services.requests import RequestFields, create_request, recipients
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.tasks import dispatch_outbox
from apps.tenants.models import Invite, TenantRole
from tests.delivery.smtp import FakeSMTP
from tests.portal.request_fixtures import PortalSetup, portal_setup

pytestmark = pytest.mark.django_db(transaction=True)

ACCEPT = re.compile(rb"/convites/aceitar/([A-Za-z0-9_\-]+)/")


@pytest.fixture
def setup(settings: SettingsWrapper) -> PortalSetup:
    settings.DELIVERY_PAUSED = False
    return portal_setup("seatless")


def _add_contacts(setup: PortalSetup) -> tuple[ClientContact, ClientContact, Invite]:
    with tenant_context(setup.graph.tenant.pk):
        invited = ClientContact.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            name="Seu João",
            email="joao@seatless.test",
        )
        nobody = ClientContact.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            name="Ana Sem Acesso",
            email="ana@seatless.test",
        )
    invite, _raw = issue_client_invite(
        actor=setup.graph.actor,
        tenant=setup.graph.tenant,
        client=setup.graph.client,
        email=invited.email,
        role=TenantRole.CLIENT_COLLABORATOR,
    )
    return invited, nobody, invite


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


def test_preview_classifies_every_contact(setup: PortalSetup) -> None:
    invited, nobody, invite = _add_contacts(setup)
    with tenant_context(setup.graph.tenant.pk):
        rows = {row.contact.pk: row for row in recipients(setup.graph.client)}
    assert rows[setup.contact.pk].access == "portal"
    assert rows[invited.pk].access == "invite"
    assert rows[invited.pk].invite == invite
    assert rows[nobody.pk].access == "ineligible"
    assert rows[nobody.pk].eligible is False
    page = setup.firm().get(f"/ciclos/{setup.cycle.pk}/")
    assert page.status_code == HTTPStatus.OK
    body = page.content.decode()
    assert f'data-access="ineligible">{nobody.name}' in body
    assert "não receberá: sem acesso ao portal e sem convite pendente" in body
    assert f'data-access="invite">{invited.name}' in body


def test_send_messages_eligible_contacts_and_embeds_the_invite_link(
    setup: PortalSetup,
) -> None:
    invited, nobody, invite = _add_contacts(setup)
    with tenant_context(setup.graph.tenant.pk):
        cycle = setup.cycle
        cycle.refresh_from_db()
        create_request(
            setup.graph.actor,
            cycle.pk,
            RequestFields(title="Extrato de outubro"),
            expected_version=cycle.version,
        )
        cycle.refresh_from_db()
    response = setup.firm().post(
        f"/ciclos/{cycle.pk}/solicitacoes/",
        {"action": "send", "expected_version": cycle.version},
    )
    assert response.status_code == HTTPStatus.FOUND
    with tenant_context(setup.graph.tenant.pk):
        latest = OutboundMessage.objects.filter(
            kind="request_notice", created_at__gte=timezone.now() - timedelta(minutes=5)
        )
        by_contact = {message.contact_id: message for message in latest}
    assert nobody.pk not in by_contact
    assert by_contact[invited.pk].invite_id == invite.pk
    with FakeSMTP() as server, _smtp(server.port):
        dispatch_outbox()
    bodies = {}
    for raw in server.received:
        parsed = email.message_from_bytes(raw)
        payload = parsed.get_payload(decode=True)
        assert isinstance(payload, bytes)
        bodies[str(parsed["To"])] = payload
    assert "ana@seatless.test" not in bodies
    assert ACCEPT.search(bodies[invited.email]) is not None
    assert ACCEPT.search(bodies[setup.contact.email]) is None
    with tenant_context(setup.graph.tenant.pk):
        assert {
            message.state
            for message in OutboundMessage.objects.filter(kind="request_notice")
        } == {MessageState.ACCEPTED}


def test_send_with_no_eligible_contact_is_refused_and_writes_nothing(
    setup: PortalSetup,
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        ClientContact.objects.filter(pk=setup.contact.pk).update(active=False)
        nobody = ClientContact.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            name="Ana Sem Acesso",
            email="ana@seatless.test",
        )
        cycle = setup.cycle
        cycle.refresh_from_db()
        draft = create_request(
            setup.graph.actor,
            cycle.pk,
            RequestFields(title="Extrato de outubro"),
            expected_version=cycle.version,
        )
        cycle.refresh_from_db()
        before = OutboundMessage.objects.count()
    response = setup.firm().post(
        f"/ciclos/{cycle.pk}/solicitacoes/",
        {"action": "send", "expected_version": cycle.version},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_CONTENT
    assert "Convide um contato antes de enviar" in response.content.decode()
    with tenant_context(setup.graph.tenant.pk):
        assert OutboundMessage.objects.count() == before
        assert setup.reload(draft).state == "rascunho"
        assert not OutboundMessage.objects.filter(contact=nobody).exists()
