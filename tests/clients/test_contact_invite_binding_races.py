"""Runtime-role invitation binding races use events, never timing delays."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from typing import Final

import pytest
from django.db import connection, connections
from django.utils import timezone

from apps.accounts import invites
from apps.accounts.invites import (
    InviteAlreadyAcceptedError,
    InviteExpiredError,
    InviteNotFoundError,
    InviteScope,
    accept_invite,
)
from apps.accounts.models import User
from apps.audit.models import AuditAction, PlatformEvent
from apps.clients.maintenance import ContactData, save_contact
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.tenants.models import Invite, Membership
from tests.clients.contact_invite_fixtures import (
    EMAIL,
    assert_unaccepted,
    contact_offer,
    stored_contact,
    verified_invitee,
)

pytestmark = pytest.mark.django_db(transaction=True)
TIMEOUT: Final = 20.0


def test_contact_edit_committing_first_refuses_waiting_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offer = contact_offer()
    user = verified_invitee()
    entering_seat = Event()
    original = invites._client_seat

    def observed(invite: Invite, account: User) -> Membership:
        entering_seat.set()
        return original(invite, account)

    monkeypatch.setattr(invites, "_client_seat", observed)

    def redeem() -> Membership:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
                cursor.execute("SET statement_timeout = '20s'")
            return accept_invite(
                invite=offer.invite, user=user, expected_scope=InviteScope.CLIENT
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with tenant_context(offer.firm.tenant.pk):
            ClientCompany.objects.select_for_update().get(pk=offer.contact.client_id)
            future = pool.submit(redeem)
            assert entering_seat.wait(TIMEOUT)
            save_contact(
                actor=offer.firm.owner,
                tenant_id=offer.firm.tenant.pk,
                client_id=offer.contact.client_id,
                contact_id=offer.contact.pk,
                expected_version=1,
                data=ContactData(name="Invited contact", email=EMAIL, active=False),
            )
        with pytest.raises(InviteNotFoundError):
            future.result(timeout=TIMEOUT)
    assert_unaccepted(offer)


def test_two_redemptions_commit_one_link_and_one_acceptance_event() -> None:
    offer = contact_offer()
    user = verified_invitee()
    start = Barrier(2, timeout=TIMEOUT)

    def redeem() -> str:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
                cursor.execute("SET statement_timeout = '20s'")
            start.wait()
            try:
                accept_invite(
                    invite=offer.invite,
                    user=user,
                    expected_scope=InviteScope.CLIENT,
                )
            except InviteAlreadyAcceptedError:
                return "already_accepted"
            return "accepted"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(redeem) for _ in range(2)]
        results = [future.result(timeout=TIMEOUT) for future in futures]
    assert sorted(results) == ["accepted", "already_accepted"]
    contact = stored_contact(offer)
    assert contact.membership_id is not None
    assert contact.version == 2
    assert (
        Membership.objects.filter(
            user=user, tenant=offer.firm.tenant, client_id=offer.contact.client_id
        ).count()
        == 1
    )
    assert (
        PlatformEvent.objects.filter(
            action=AuditAction.INVITE_ACCEPTED, metadata__invite_id=str(offer.invite.pk)
        ).count()
        == 1
    )


def test_expiry_while_waiting_for_contact_authority_rolls_back_every_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offer = contact_offer()
    user = verified_invitee()
    clock = [timezone.now()]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    entering_seat = Event()
    original = invites._client_seat

    def observed(invite: Invite, account: User) -> Membership:
        entering_seat.set()
        return original(invite, account)

    monkeypatch.setattr(invites, "_client_seat", observed)

    def redeem() -> Membership:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
                cursor.execute("SET statement_timeout = '20s'")
            return accept_invite(
                invite=offer.invite, user=user, expected_scope=InviteScope.CLIENT
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with tenant_context(offer.firm.tenant.pk):
            ClientCompany.objects.select_for_update().get(pk=offer.contact.client_id)
            future = pool.submit(redeem)
            assert entering_seat.wait(TIMEOUT)
            clock[0] = offer.invite.expires_at + timedelta(seconds=1)
        with pytest.raises(InviteExpiredError):
            future.result(timeout=TIMEOUT)
    assert_unaccepted(offer)
