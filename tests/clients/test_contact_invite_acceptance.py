"""Acceptance binds only the explicitly invited contact, in the grant transaction."""

from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.invites import (
    InviteAlreadyAcceptedError,
    InviteEmailMismatchError,
    InviteError,
    InviteExpiredError,
    InviteNotFoundError,
    InviteRevokedError,
    InviteScope,
    InviteTokenSupersededError,
    accept_invite,
    issue_client_invite,
    mint_token_for_dispatch,
    register_and_accept,
    resolve_invite,
)
from apps.accounts.models import User
from apps.audit.models import AuditAction, PlatformEvent
from apps.clients.maintenance import ContactData, invite_contact, save_contact
from apps.clients.models import ClientContact
from apps.core.tenancy import current_tenant_id, tenant_context
from apps.tenants.models import Invite, Membership, TenantRole
from tests.clients.contact_invite_fixtures import (
    EMAIL,
    assert_unaccepted,
    contact_offer,
    redeem,
    stored_contact,
    verified_invitee,
)
from tests.ui.factories import PASSWORD, make_firm

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("existing", [False, True])
def test_acceptance_binds_the_origin_not_other_contacts(existing: bool) -> None:
    # Given an origin, an earlier same-address contact, and other client/firm rows.
    offer = contact_offer()
    beta = make_firm("other-contact", client_count=1)
    for firm, company in [
        (offer.firm, offer.firm.clients[1]),
        (beta, beta.clients[0]),
    ]:
        with tenant_context(firm.tenant.pk):
            save_contact(
                actor=firm.owner,
                tenant_id=firm.tenant.pk,
                client_id=company.pk,
                data=ContactData(name="Unrelated", email=EMAIL),
            )
    assert current_tenant_id.get() is None

    # When acceptance starts without tenant context, as on the portal invite door.
    if existing:
        user = verified_invitee()
        member = accept_invite(
            invite=resolve_invite(offer.token),
            user=user,
            expected_scope=InviteScope.CLIENT,
        )
    else:
        user, member = register_and_accept(
            invite=resolve_invite(offer.token),
            password=PASSWORD,
            expected_scope=InviteScope.CLIENT,
        )

    # Then the active seat and origin link commit together, without any fixture link.
    stored = stored_contact(offer)
    assert (stored.membership_id, stored.version, member.is_active) == (
        member.pk,
        2,
        True,
    )
    assert (member.tenant_id, member.client_id, member.user_id, member.role) == (
        offer.firm.tenant.pk,
        offer.contact.client_id,
        user.pk,
        TenantRole.CLIENT_OWNER,
    )
    for firm in [offer.firm, beta]:
        with tenant_context(firm.tenant.pk):
            assert (
                not ClientContact.objects.exclude(pk=offer.contact.pk)
                .filter(membership__isnull=False)
                .exists()
            )
    assert current_tenant_id.get() is None
    assert (
        PlatformEvent.objects.filter(
            action=AuditAction.INVITE_ACCEPTED, metadata__invite_id=str(offer.invite.pk)
        ).count()
        == 1
    )


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize(
    "data",
    [
        ContactData(name="Invited contact", email=EMAIL, active=False),
        ContactData(name="Invited contact", email=EMAIL, channel_email_allowed=False),
        ContactData(name="Invited contact", email="changed@contact.test"),
    ],
)
def test_changed_origin_rolls_back_the_whole_acceptance(
    existing: bool, data: ContactData
) -> None:
    # Given a mailed invitation whose origin subsequently loses eligibility.
    offer = contact_offer()
    user = verified_invitee() if existing else None
    with tenant_context(offer.firm.tenant.pk):
        save_contact(
            actor=offer.firm.owner,
            tenant_id=offer.firm.tenant.pk,
            client_id=offer.contact.client_id,
            contact_id=offer.contact.pk,
            expected_version=offer.contact.version,
            data=data,
        )

    # When the holder accepts the original credential.
    with pytest.raises(InviteNotFoundError):
        redeem(offer.invite, user)

    # Then neither an account created by this attempt nor a seat/link survives.
    assert_unaccepted(offer)
    assert User.objects.filter(email=EMAIL).exists() is existing
    assert EmailAddress.objects.filter(email=EMAIL, verified=True).exists() is existing
    assert not PlatformEvent.objects.filter(
        action=AuditAction.INVITE_ACCEPTED, metadata__invite_id=str(offer.invite.pk)
    ).exists()
    assert current_tenant_id.get() is None


@pytest.mark.parametrize(
    ("field", "error"),
    [("expires_at", InviteExpiredError), ("revoked_at", InviteRevokedError)],
)
def test_dead_invitation_cannot_bind(field: str, error: type[InviteError]) -> None:
    offer = contact_offer()
    user = verified_invitee()
    Invite.objects.filter(pk=offer.invite.pk).update(
        **{field: timezone.now() - timedelta(days=1)}
    )
    with pytest.raises(error):
        accept_invite(invite=offer.invite, user=user, expected_scope=InviteScope.CLIENT)
    assert_unaccepted(offer)


@pytest.mark.parametrize("existing", [False, True])
def test_refresh_after_resolution_refuses_the_stale_token(existing: bool) -> None:
    offer = contact_offer()
    resolved = resolve_invite(offer.token)
    user = verified_invitee() if existing else None
    with transaction.atomic():
        mint_token_for_dispatch(offer.invite)
    with pytest.raises(InviteTokenSupersededError):
        redeem(resolved, user)
    assert_unaccepted(offer)
    assert User.objects.filter(email=EMAIL).exists() is existing


@pytest.mark.parametrize("verified", [False, True])
def test_wrong_or_unverified_mailbox_cannot_bind(verified: bool) -> None:
    offer = contact_offer()
    user = User.objects.create_user(email="other@contact.test")
    EmailAddress.objects.create(
        user=user, email=user.email if verified else EMAIL, verified=verified
    )
    with pytest.raises(InviteEmailMismatchError):
        accept_invite(invite=offer.invite, user=user, expected_scope=InviteScope.CLIENT)
    assert_unaccepted(offer)


def test_email_only_invitation_does_not_guess_a_contact_origin() -> None:
    offer = contact_offer()
    user = verified_invitee()
    invite, _token = issue_client_invite(
        actor=offer.firm.owner,
        tenant=offer.firm.tenant,
        client=offer.firm.clients[0],
        email=EMAIL,
        role=TenantRole.CLIENT_OWNER,
    )
    accept_invite(invite=invite, user=user, expected_scope=InviteScope.CLIENT)
    assert stored_contact(offer).membership_id is None


def test_reinvite_reactivates_the_same_seat_without_rebinding_or_replay() -> None:
    offer = contact_offer()
    user = verified_invitee()
    first = accept_invite(
        invite=offer.invite, user=user, expected_scope=InviteScope.CLIENT
    )
    Membership.objects.filter(pk=first.pk).update(is_active=False)
    contact = stored_contact(offer)
    with tenant_context(offer.firm.tenant.pk):
        invite, _token = invite_contact(
            actor=offer.firm.owner,
            tenant=offer.firm.tenant,
            client_id=contact.client_id,
            contact_id=contact.pk,
            expected_version=contact.version,
            role=TenantRole.CLIENT_COLLABORATOR,
        )
    second = accept_invite(invite=invite, user=user, expected_scope=InviteScope.CLIENT)
    assert (second.pk, second.is_active, second.role) == (
        first.pk,
        True,
        TenantRole.CLIENT_COLLABORATOR,
    )
    assert stored_contact(offer).version == contact.version
    with pytest.raises(InviteAlreadyAcceptedError):
        accept_invite(invite=invite, user=user, expected_scope=InviteScope.CLIENT)
    assert stored_contact(offer).membership_id == first.pk


def test_invitation_origin_cannot_cross_the_company_boundary() -> None:
    offer = contact_offer()
    with tenant_context(offer.firm.tenant.pk):
        other = save_contact(
            actor=offer.firm.owner,
            tenant_id=offer.firm.tenant.pk,
            client_id=offer.firm.clients[1].pk,
            data=ContactData(name="Another company", email=EMAIL),
        )
    with (
        pytest.raises(IntegrityError, match="invite_tenant_client_contact_fk"),
        transaction.atomic(),
    ):
        Invite.objects.filter(pk=offer.invite.pk).update(contact_id=other.pk)
    assert_unaccepted(offer)
