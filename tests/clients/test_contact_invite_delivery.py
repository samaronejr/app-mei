"""Contact portal invites: one credential, current permissions, durable dispatch.

Review blockers of AXR1-007 (B1, B2, B4), each reproduced through the real
authenticated route:

* two requests racing past the page's read must not mint two invitations or two mails;
* a request whose page was read before the contact lost its e-mail permission must be
  refused, with no invitation and no mail;
* the mail leaves only after the database transaction committed (no external I/O
  inside the tenant transaction);
* a known SMTP refusal withdraws the undelivered invitation; failed cleanup
  remains visible and can be recovered without sending again.

Synchronization is by `threading.Barrier`/direct sequencing with bounded timeouts; no
sleeps. Every seam calls the real implementation.
"""

import threading
from collections.abc import Callable
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any, Final

import pytest
from django.core import mail
from django.db import connection, connections
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.audit.models import AuditAction, PlatformEvent
from apps.clients import views_onboarding
from apps.clients.maintenance import ContactData, save_contact
from apps.clients.models import ClientCompany, ClientContact
from apps.core.tenancy import tenant_context
from apps.delivery import gateway, kinds
from apps.delivery.contracts import Reply
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.notice_authority import configure
from apps.delivery.services import claim_batch, finalize
from apps.delivery.tasks import dispatch_outbox
from apps.tenants.models import Invite, Membership, TenantRole
from tests.delivery.smtp import FakeSMTP
from tests.delivery.test_portal_invite_outbox import _smtp
from tests.support_threads import run_in_thread_as_runtime
from tests.ui.factories import Firm, add_member, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

EMAIL: Final = "maria@padaria.test"
TIMEOUT: Final = 20.0


@pytest.fixture(autouse=True)
def delivery_storage(settings: SettingsWrapper, tmp_path: Path) -> None:
    settings.DELIVERY_PAUSED = False
    settings.MEDIA_ROOT = tmp_path


def _firm_with_contact(slug: str) -> tuple[Firm, ClientCompany, ClientContact]:
    firm = make_firm(slug, client_count=1)
    firm.tenant.features = ["import_r1", "cycles_r1"]
    firm.tenant.save(update_fields=["features"])
    client = firm.clients[0]
    with tenant_context(firm.tenant.id):
        contact = ClientContact.objects.create(
            tenant=firm.tenant,
            client=client,
            name="Dona Maria",
            email=EMAIL,
        )
        configure(
            firm.owner,
            kinds=("portal_invite",),
            acknowledged=True,
            expected_version=0,
        )
    return firm, client, contact


def _invite_url(client: ClientCompany, contact: ClientContact) -> str:
    return reverse("client-contact-invite", args=[client.pk, contact.pk])


def _payload(version: int = 1) -> dict[str, Any]:
    return {"convite-role": TenantRole.CLIENT_OWNER, "convite-version": version}


def _live_invites(firm: Firm) -> int:
    # PLATFORM_QUERY_OK: exact synthetic tenant; the issuer may have lost access.
    return Invite.objects.filter(
        tenant=firm.tenant, revoked_at__isnull=True, accepted_at__isnull=True
    ).count()


def _message(firm: Firm) -> OutboundMessage:
    with tenant_context(firm.tenant.pk):
        return OutboundMessage.objects.latest("created_at")


def _dispatch(mode: str = "accept") -> list[bytes]:
    with FakeSMTP(mode=mode) as server, _smtp(server.port):
        dispatch_outbox()
    return server.received


def _wrap(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    after: Callable[[], object],
) -> None:
    """Replace `views_onboarding.<name>` with the real call followed by `after()`."""
    real = getattr(views_onboarding, name)

    def wrapped(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        result = real(*args, **kwargs)
        after()
        return result

    monkeypatch.setattr(views_onboarding, name, wrapped)


def test_two_racing_invites_mint_one_invitation_and_one_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firm, client, contact = _firm_with_contact("corrida")
    signed_in = firm.as_owner()
    barrier = threading.Barrier(2, timeout=TIMEOUT)
    # Both requests have read the contact (and passed every check made on that read)
    # before either proceeds: exactly the window a check-before-insert loses.
    _wrap(monkeypatch, "load_contact", barrier.wait)
    mail.outbox.clear()
    statuses: list[int] = []
    errors: list[BaseException] = []

    def post() -> None:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
            browser = Client(SERVER_NAME=firm.host)
            browser.cookies.load(
                {name: morsel.value for name, morsel in signed_in.cookies.items()},
            )
            statuses.append(
                browser.post(_invite_url(client, contact), _payload()).status_code,
            )
        except BaseException as error:  # noqa: BLE001 - re-raised below
            errors.append(error)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=post) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(TIMEOUT * 2)
    assert not any(thread.is_alive() for thread in threads), "a request hung"
    assert errors == []

    assert sorted(statuses) == [HTTPStatus.FOUND, HTTPStatus.CONFLICT]
    assert _live_invites(firm) == 1
    assert mail.outbox == []
    with tenant_context(firm.tenant.pk):
        assert OutboundMessage.objects.count() == 1
    assert len(_dispatch()) == 1
    assert _message(firm).recipients_json == [EMAIL]


def test_a_permission_revoked_after_the_page_read_stops_the_invite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firm, client, contact = _firm_with_contact("revogado")
    browser = firm.as_owner()

    def revoke_on_another_connection() -> None:
        run_in_thread_as_runtime(
            firm.tenant.id,
            lambda: save_contact(
                actor=firm.owner,
                tenant_id=firm.tenant.id,
                client_id=client.pk,
                data=ContactData(
                    name="Dona Maria",
                    email=EMAIL,
                    channel_email_allowed=False,
                    active=False,
                ),
                contact_id=contact.pk,
                expected_version=1,
            ),
            timeout=TIMEOUT,
        )

    # The request has read the contact; before it acts, the contact is deactivated
    # and its e-mail permission withdrawn on a second connection.
    _wrap(monkeypatch, "load_contact", revoke_on_another_connection)
    mail.outbox.clear()

    response = browser.post(_invite_url(client, contact), _payload(version=1))

    assert response.status_code == HTTPStatus.CONFLICT
    assert _live_invites(firm) == 0
    assert mail.outbox == []
    with tenant_context(firm.tenant.id):
        stored = ClientContact.objects.get(pk=contact.pk)
    assert (stored.active, stored.channel_email_allowed, stored.version) == (
        False,
        False,
        2,
    )


def test_the_invitation_is_mailed_only_after_the_transaction_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firm, client, contact = _firm_with_contact("depois")
    seen: list[tuple[bool, int]] = []
    real = gateway.smtp_send

    def observing(*args: Any, **kwargs: Any) -> Reply:  # noqa: ANN401
        seen.append((connection.in_atomic_block, _live_invites(firm)))
        return real(*args, **kwargs)

    monkeypatch.setattr(gateway, "smtp_send", observing)
    mail.outbox.clear()

    response = firm.as_owner().post(_invite_url(client, contact), _payload())

    assert response.status_code == HTTPStatus.FOUND
    assert seen == []
    assert mail.outbox == []
    assert len(_dispatch()) == 1
    # Not in an atomic block, and the invitation is already committed and visible
    # to a fresh read when the mail leaves.
    assert seen == [(False, 1)]
    assert _message(firm).recipients_json == [EMAIL]


def test_an_smtp_refusal_is_a_retryable_message_and_leaves_no_live_invite() -> None:
    firm, client, contact = _firm_with_contact("smtp")
    browser = firm.as_owner()

    response = browser.post(_invite_url(client, contact), _payload())

    assert response.status_code == HTTPStatus.FOUND
    assert len(_dispatch("reject")) == 1
    assert _message(firm).state == MessageState.REJECTED
    page = browser.get(response["Location"])
    assert page.status_code == HTTPStatus.OK
    content = page.content.decode()
    assert _live_invites(firm) == 0
    # The contact can be invited again: the retry control is back on the page.
    assert "Convidar Dona Maria para o portal" in content

    mail.outbox.clear()
    retry = browser.post(_invite_url(client, contact), _payload())
    assert retry.status_code == HTTPStatus.FOUND
    assert _live_invites(firm) == 1
    assert mail.outbox == []
    assert len(_dispatch()) == 1


def test_an_expired_invitation_does_not_block_a_new_one() -> None:
    firm, client, contact = _firm_with_contact("expirado")
    browser = firm.as_owner()
    assert browser.post(_invite_url(client, contact), _payload()).status_code == (
        HTTPStatus.FOUND
    )
    Invite.objects.for_user(firm.owner).filter(tenant=firm.tenant).update(
        expires_at=timezone.now() - timedelta(days=1),
    )
    again = browser.post(_invite_url(client, contact), _payload())
    assert again.status_code == HTTPStatus.FOUND
    assert Invite.objects.for_user(firm.owner).filter(tenant=firm.tenant).count() == 2


def test_a_refused_send_is_withdrawn_even_if_the_requester_lost_the_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compensation is system cleanup of that one invite, not the requester's act."""
    firm, client, contact = _firm_with_contact("perdeu")
    admin = add_member(firm.tenant, "admin@perdeu.example.com", TenantRole.OWNER)
    owner_browser = firm.as_owner()
    admin_browser = firm.sign_in(admin)

    real = gateway.smtp_send

    def demote_then_refuse(*args: Any, **kwargs: Any) -> Reply:  # noqa: ANN401
        # Dispatch start is already committed. A later demotion cannot undo I/O
        # or prevent system compensation of that exact provider-refused invite.
        # PLATFORM_QUERY_OK: the test's own membership row.
        Membership.objects.filter(user=firm.owner, tenant=firm.tenant).update(
            role=TenantRole.STAFF_ACCOUNTANT,
        )
        return real(*args, **kwargs)

    monkeypatch.setattr(gateway, "smtp_send", demote_then_refuse)
    mail.outbox.clear()

    response = owner_browser.post(_invite_url(client, contact), _payload())

    assert response.status_code == HTTPStatus.FOUND
    assert len(_dispatch("reject")) == 1
    assert _live_invites(firm) == 0
    page = admin_browser.get(response["Location"]).content.decode()
    assert "Convite enviado" not in page
    withdrawn = PlatformEvent.objects.filter(
        action=AuditAction.INVITE_REVOKED,
        tenant_id=firm.tenant.id,
    )
    assert [(e.actor_id, e.metadata.get("system")) for e in withdrawn] == [
        (firm.owner.pk, True),
    ]

    monkeypatch.undo()
    with tenant_context(firm.tenant.pk):
        configure(
            admin,
            kinds=("portal_invite",),
            acknowledged=True,
            expected_version=1,
        )
    retry = admin_browser.post(_invite_url(client, contact), _payload())
    assert retry.status_code == HTTPStatus.FOUND
    assert _live_invites(firm) == 1
    assert mail.outbox == []
    assert len(_dispatch()) == 1


def test_a_failed_withdrawal_is_reported_truthfully_and_can_be_recovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firm, client, contact = _firm_with_contact("irresolvido")
    browser = firm.as_owner()

    def cleanup_down(*_args: Any, **_kwargs: Any) -> None:  # noqa: ANN401
        msg = "database unavailable for the compensation"
        raise RuntimeError(msg)

    monkeypatch.setattr(kinds, "withdraw_undelivered_invite", cleanup_down)
    mail.outbox.clear()

    response = browser.post(_invite_url(client, contact), _payload())

    assert response.status_code == HTTPStatus.FOUND
    claim = claim_batch(firm.tenant.pk)[0]
    with FakeSMTP(mode="reject") as server, _smtp(server.port):
        reply = gateway.send(claim)
    assert reply.state == MessageState.REJECTED
    assert len(server.received) == 1
    with pytest.raises(RuntimeError, match="database unavailable"):
        finalize(claim, reply)
    assert _live_invites(firm) == 1
    page = browser.get(response["Location"]).content.decode()
    assert "Nenhum convite ficou ativo" not in page
    assert "Convite enviado" not in page
    # The recovery path is on the page: cancel the pending invitation, then retry.
    invite = Invite.objects.for_user(firm.owner).get(tenant=firm.tenant)
    revoke_url = reverse("invite-revoke", args=[invite.pk])
    assert f'href="{revoke_url}"' in page

    monkeypatch.undo()
    blocked = browser.post(_invite_url(client, contact), _payload())
    assert blocked.status_code == HTTPStatus.CONFLICT
    assert browser.post(revoke_url).status_code == HTTPStatus.FOUND
    assert _live_invites(firm) == 0
    assert finalize(claim, reply)
    retry = browser.post(_invite_url(client, contact), _payload())
    assert retry.status_code == HTTPStatus.FOUND
    assert _live_invites(firm) == 1
    assert mail.outbox == []
    assert len(_dispatch()) == 1
