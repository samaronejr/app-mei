"""Contact portal invites: one credential, current permissions, mail after commit.

Review blockers of AXR1-007 (B1, B2, B4), each reproduced through the real
authenticated route:

* two requests racing past the page's read must not mint two invitations or two mails;
* a request whose page was read before the contact lost its e-mail permission must be
  refused, with no invitation and no mail;
* the mail leaves only after the database transaction committed (no external I/O
  inside the tenant transaction);
* an SMTP refusal is a recoverable pt-BR message, never a 500, and leaves no live
  invitation behind.

Synchronization is by `threading.Barrier`/direct sequencing with bounded timeouts; no
sleeps. Every seam calls the real implementation.
"""

import threading
from collections.abc import Callable
from datetime import timedelta
from http import HTTPStatus
from smtplib import SMTPException
from typing import Any, Final

import pytest
from django.core import mail
from django.db import connection, connections
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.clients import views_onboarding
from apps.clients.maintenance import ContactData, save_contact
from apps.clients.models import ClientCompany, ClientContact
from apps.core.tenancy import tenant_context
from apps.tenants.models import Invite, TenantRole
from tests.support_threads import run_in_thread_as_runtime
from tests.ui.factories import Firm, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

EMAIL: Final = "maria@padaria.test"
TIMEOUT: Final = 20.0


def _firm_with_contact(slug: str) -> tuple[Firm, ClientCompany, ClientContact]:
    firm = make_firm(slug, client_count=1)
    firm.tenant.features = ["import_r1"]
    firm.tenant.save(update_fields=["features"])
    client = firm.clients[0]
    with tenant_context(firm.tenant.id):
        contact = ClientContact.objects.create(
            tenant=firm.tenant,
            client=client,
            name="Dona Maria",
            email=EMAIL,
        )
    return firm, client, contact


def _invite_url(client: ClientCompany, contact: ClientContact) -> str:
    return reverse("client-contact-invite", args=[client.pk, contact.pk])


def _payload(version: int = 1) -> dict[str, Any]:
    return {"convite-role": TenantRole.CLIENT_OWNER, "convite-version": version}


def _live_invites(firm: Firm) -> int:
    return (
        Invite.objects.for_user(firm.owner)
        .filter(tenant=firm.tenant, revoked_at__isnull=True, accepted_at__isnull=True)
        .count()
    )


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
    assert [message.to for message in mail.outbox] == [[EMAIL]]


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
    real = views_onboarding._mail_contact_invitation

    def observing(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        seen.append((connection.in_atomic_block, _live_invites(firm)))
        real(*args, **kwargs)

    monkeypatch.setattr(views_onboarding, "_mail_contact_invitation", observing)
    mail.outbox.clear()

    response = firm.as_owner().post(_invite_url(client, contact), _payload())

    assert response.status_code == HTTPStatus.FOUND
    # Not in an atomic block, and the invitation is already committed and visible
    # to a fresh read when the mail leaves.
    assert seen == [(False, 1)]
    assert [message.to for message in mail.outbox] == [[EMAIL]]


def test_an_smtp_refusal_is_a_retryable_message_and_leaves_no_live_invite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firm, client, contact = _firm_with_contact("smtp")
    browser = firm.as_owner()

    def refuse(*_args: Any, **_kwargs: Any) -> None:  # noqa: ANN401
        msg = "relay refused"
        raise SMTPException(msg)

    monkeypatch.setattr(views_onboarding, "_mail_contact_invitation", refuse)

    response = browser.post(_invite_url(client, contact), _payload())

    assert response.status_code == HTTPStatus.FOUND
    page = browser.get(response["Location"])
    assert page.status_code == HTTPStatus.OK
    content = page.content.decode()
    assert "Não foi possível enviar o convite por e-mail" in content
    assert _live_invites(firm) == 0
    # The contact can be invited again: the retry control is back on the page.
    assert "Convidar Dona Maria para o portal" in content

    monkeypatch.undo()
    mail.outbox.clear()
    retry = browser.post(_invite_url(client, contact), _payload())
    assert retry.status_code == HTTPStatus.FOUND
    assert _live_invites(firm) == 1
    assert len(mail.outbox) == 1


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
