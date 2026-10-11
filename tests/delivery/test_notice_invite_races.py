"""CLIENT acceptance and dispatch compose under real runtime row locks."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from django.core.mail import EmailMessage
from django.db import OperationalError, connection
from pytest_django.fixtures import SettingsWrapper

from apps.accounts import invites
from apps.accounts.invites import InviteTokenSupersededError
from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.delivery import gateway, journal
from apps.delivery.contracts import Reply
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.notice_authority import configure
from apps.delivery.outbox import enqueue_portal_invite
from apps.delivery.services import claim_batch, finalize
from apps.tenants.models import Invite, Membership
from tests.clients.contact_invite_fixtures import (
    EMAIL,
    ContactOffer,
    contact_offer,
    redeem,
    stored_contact,
    verified_invitee,
)
from tests.delivery.smtp import FakeSMTP
from tests.delivery.test_notice_dispatch import _runtime

pytestmark = pytest.mark.django_db(transaction=True)


def _queue(
    settings: SettingsWrapper, tmp_path: Path
) -> tuple[ContactOffer, OutboundMessage]:
    settings.DELIVERY_PAUSED = False
    settings.MEDIA_ROOT = tmp_path
    offer = contact_offer()
    offer.firm.tenant.features = ["cycles_r1", "import_r1"]
    offer.firm.tenant.save(update_fields=["features"])
    with tenant_context(offer.firm.tenant.pk):
        configure(
            offer.firm.owner,
            kinds=("portal_invite",),
            acknowledged=True,
            expected_version=0,
        )
        message = enqueue_portal_invite(offer.firm.owner, offer.invite, offer.contact)
    return offer, message


@pytest.mark.parametrize("existing_account", [True, False])
def test_acceptance_commits_first_dispatch_refuses_without_data(
    settings: SettingsWrapper,
    tmp_path: Path,
    smtp_server: Callable[[str], FakeSMTP],
    existing_account: bool,
) -> None:
    offer, message = _queue(settings, tmp_path)
    user = verified_invitee() if existing_account else None
    claim = claim_batch(offer.firm.tenant.pk)[0]
    held, release = Event(), Event()
    server = smtp_server("accept")

    def accept() -> Membership:
        with tenant_context(offer.firm.tenant.pk):
            seat = redeem(offer.invite, user)
            held.set()
            assert release.wait(15)
        return seat

    with ThreadPoolExecutor(max_workers=2) as pool:
        accepter = pool.submit(_runtime, accept)
        try:
            assert held.wait(15)
            contender = pool.submit(_runtime, lambda: gateway.send(claim), timeout=True)
            with pytest.raises(OperationalError) as error:
                contender.result(timeout=15)
            assert getattr(error.value.__cause__, "sqlstate", None) == "55P03"
        finally:
            release.set()
        seat = accepter.result(timeout=15)
    reply = gateway.send(claim)
    assert reply.state == MessageState.CANCELLED
    assert reply.code == "invite_accepted"
    assert not finalize(claim, reply)
    assert server.received == []
    assert journal.read(offer.firm.tenant.pk) == []
    assert stored_contact(offer).membership_id == seat.pk
    assert Invite.objects.get(pk=offer.invite.pk).accepted_at is not None
    with tenant_context(offer.firm.tenant.pk):
        message.refresh_from_db()
        assert message.send_started_at is None


@pytest.mark.parametrize("existing_account", [True, False])
def test_dispatch_start_first_preserves_stale_token_guard_and_one_send(
    settings: SettingsWrapper,
    tmp_path: Path,
    smtp_server: Callable[[str], FakeSMTP],
    monkeypatch: pytest.MonkeyPatch,
    existing_account: bool,
) -> None:
    offer, message = _queue(settings, tmp_path)
    user = verified_invitee() if existing_account else None
    claim = claim_batch(offer.firm.tenant.pk)[0]
    minted, release_authority = Event(), Event()
    io_reached, release_io = Event(), Event()
    original_mint, original_smtp = invites.mint_token_for_dispatch, gateway.smtp_send

    def held_mint(invite: Invite) -> str:
        raw = original_mint(invite)
        minted.set()
        assert release_authority.wait(15)
        return raw

    def held_smtp(email: EmailMessage) -> Reply:
        assert not connection.in_atomic_block
        io_reached.set()
        assert release_io.wait(15)
        return original_smtp(email)

    def deliver() -> Reply:
        reply = gateway.send(claim)
        assert finalize(claim, reply)
        return reply

    monkeypatch.setattr(invites, "mint_token_for_dispatch", held_mint)
    monkeypatch.setattr(gateway, "smtp_send", held_smtp)
    server = smtp_server("accept")
    with ThreadPoolExecutor(max_workers=2) as pool:
        dispatcher = pool.submit(_runtime, deliver)
        try:
            assert minted.wait(15)
            contender = pool.submit(
                _runtime, lambda: redeem(offer.invite, user), timeout=True
            )
            with pytest.raises(OperationalError) as error:
                contender.result(timeout=15)
            assert getattr(error.value.__cause__, "sqlstate", None) == "55P03"
            release_authority.set()
            assert io_reached.wait(15)
            with pytest.raises(InviteTokenSupersededError):
                redeem(offer.invite, user)
            assert stored_contact(offer).membership_id is None
            assert Invite.objects.get(pk=offer.invite.pk).accepted_at is None
            if not existing_account:
                assert not User.objects.filter(email=EMAIL).exists()
            with tenant_context(offer.firm.tenant.pk):
                message.refresh_from_db()
                assert message.send_started_at is not None
            assert [
                record.outcome for record in journal.read(offer.firm.tenant.pk)
            ] == ["intent_started"]
        finally:
            release_authority.set()
            release_io.set()
        assert dispatcher.result(timeout=15).state == MessageState.ACCEPTED
    assert len(server.received) == 1
    current = Invite.objects.get(pk=offer.invite.pk)
    seat = redeem(current, user)
    assert stored_contact(offer).membership_id == seat.pk
