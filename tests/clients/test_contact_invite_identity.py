"""Mailbox proof does not authorize rewriting another contact's established seat."""

from http import HTTPStatus

import pytest
from allauth.account.models import EmailAddress
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.invites import (
    InviteAccountExistsError,
    InviteNotFoundError,
    InviteScope,
    accept_invite,
    register_and_accept,
    resolve_invite,
)
from apps.accounts.models import User
from apps.clients.maintenance import invite_contact
from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.delivery.notice_authority import configure
from apps.tenants.models import Invite, Membership, TenantRole
from tests.clients.contact_invite_fixtures import (
    EMAIL,
    assert_unaccepted,
    contact_offer,
    stored_contact,
    verified_invitee,
)
from tests.clients.contact_invite_journey import _dispatch_token
from tests.ui.factories import PASSWORD

pytestmark = pytest.mark.django_db(transaction=True)


def test_verified_secondary_mailbox_binds_the_accepting_account() -> None:
    offer = contact_offer()
    user = User.objects.create_user(email="login@another.test")
    EmailAddress.objects.create(user=user, email=EMAIL, verified=True, primary=True)
    member = accept_invite(
        invite=offer.invite, user=user, expected_scope=InviteScope.CLIENT
    )
    assert stored_contact(offer).membership_id == member.pk
    assert member.user_id == user.pk


def test_inactive_account_is_refused_using_the_fresh_user_row() -> None:
    offer = contact_offer()
    user = verified_invitee()
    User.objects.filter(pk=user.pk).update(is_active=False)
    assert user.is_active
    with pytest.raises(InviteNotFoundError):
        accept_invite(invite=offer.invite, user=user, expected_scope=InviteScope.CLIENT)
    assert_unaccepted(offer)


def test_registration_refuses_an_existing_verified_secondary_mailbox() -> None:
    offer = contact_offer()
    user = User.objects.create_user(email="login@another.test")
    EmailAddress.objects.create(user=user, email=EMAIL, verified=True, primary=True)
    with pytest.raises(InviteAccountExistsError):
        register_and_accept(
            invite=offer.invite, password=PASSWORD, expected_scope=InviteScope.CLIENT
        )
    assert_unaccepted(offer)
    assert not User.objects.filter(email=EMAIL).exists()


def test_unverified_secondary_claim_does_not_block_registration() -> None:
    offer = contact_offer()
    claimant = User.objects.create_user(email="unverified@another.test")
    EmailAddress.objects.create(
        user=claimant, email=EMAIL, verified=False, primary=True
    )
    user, member = register_and_accept(
        invite=offer.invite, password=PASSWORD, expected_scope=InviteScope.CLIENT
    )
    assert user.pk != claimant.pk
    assert stored_contact(offer).membership_id == member.pk


def test_address_only_web_invite_does_not_choose_a_person_by_email(
    settings: SettingsWrapper,
) -> None:
    offer = contact_offer()
    user = verified_invitee()
    offer.firm.tenant.features = ["cycles_r1"]
    offer.firm.tenant.save(update_fields=["features"])
    settings.DELIVERY_PAUSED = False
    with tenant_context(offer.firm.tenant.pk):
        configure(
            offer.firm.owner,
            kinds=("portal_invite",),
            acknowledged=True,
            expected_version=0,
        )
    issued = offer.firm.as_owner().post(
        reverse("portal-invite-issue", args=[offer.contact.client_id]),
        {"email": EMAIL, "role": TenantRole.CLIENT_OWNER},
    )
    assert issued.status_code == HTTPStatus.FOUND
    invite = resolve_invite(_dispatch_token())
    member = accept_invite(invite=invite, user=user, expected_scope=InviteScope.CLIENT)
    assert member.is_active
    assert member.client_id == offer.contact.client_id
    assert invite.contact_id is None
    with tenant_context(offer.firm.tenant.pk):
        assert not ClientContact.objects.filter(membership__isnull=False).exists()


def test_established_contact_binding_cannot_be_transferred_to_another_account() -> None:
    offer = contact_offer()
    original_user = verified_invitee()
    original_member = accept_invite(
        invite=offer.invite, user=original_user, expected_scope=InviteScope.CLIENT
    )
    contact = stored_contact(offer)
    # Mailbox control moves, but the firm's existing person/seat association does not.
    EmailAddress.objects.filter(user=original_user, email=EMAIL).delete()
    new_user = User.objects.create_user(email="new-login@another.test")
    EmailAddress.objects.create(user=new_user, email=EMAIL, verified=True, primary=True)
    with tenant_context(offer.firm.tenant.pk):
        invite, _token = invite_contact(
            actor=offer.firm.owner,
            tenant=offer.firm.tenant,
            client_id=contact.client_id,
            contact_id=contact.pk,
            role=TenantRole.CLIENT_OWNER,
            expected_version=contact.version,
        )
    with pytest.raises(InviteNotFoundError):
        accept_invite(invite=invite, user=new_user, expected_scope=InviteScope.CLIENT)
    assert stored_contact(offer).membership_id == original_member.pk
    assert Invite.objects.get(pk=invite.pk).accepted_at is None
    assert (
        Membership.objects.filter(
            tenant=offer.firm.tenant, client_id=contact.client_id
        ).count()
        == 1
    )
