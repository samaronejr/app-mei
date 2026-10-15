"""Contact invitations issued by the real service, without seeded seat links."""

from dataclasses import dataclass
from typing import Final

from allauth.account.models import EmailAddress

from apps.accounts.invites import InviteScope, accept_invite, register_and_accept
from apps.accounts.models import User
from apps.clients.maintenance import ContactData, invite_contact, save_contact
from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.tenants.models import Invite, Membership, TenantRole
from tests.ui.factories import PASSWORD, Firm, make_firm

EMAIL: Final = "maria@contact.test"


@dataclass(frozen=True, slots=True)
class ContactOffer:
    firm: Firm
    contact: ClientContact
    invite: Invite
    token: str


def contact_offer() -> ContactOffer:
    """Offer a seat to a contact, not to an earlier row sharing their address."""
    firm = make_firm("contact-binding", client_count=2)
    with tenant_context(firm.tenant.pk):
        save_contact(
            actor=firm.owner,
            tenant_id=firm.tenant.pk,
            client_id=firm.clients[0].pk,
            data=ContactData(name="Another contact", email=EMAIL),
        )
        contact = save_contact(
            actor=firm.owner,
            tenant_id=firm.tenant.pk,
            client_id=firm.clients[0].pk,
            data=ContactData(name="Invited contact", email=EMAIL),
        )
        invite, token = invite_contact(
            actor=firm.owner,
            tenant=firm.tenant,
            client_id=contact.client_id,
            contact_id=contact.pk,
            role=TenantRole.CLIENT_OWNER,
            expected_version=contact.version,
        )
    return ContactOffer(firm, contact, invite, token)


def verified_invitee() -> User:
    """Create an existing account with mailbox proof, but no target-company seat."""
    user = User.objects.create_user(email=EMAIL, password=PASSWORD)
    EmailAddress.objects.create(user=user, email=EMAIL, verified=True, primary=True)
    return user


def redeem(invite: Invite, user: User | None) -> Membership:
    """Run the actual existing-account or registration acceptance entry point."""
    if user is not None:
        return accept_invite(
            invite=invite, user=user, expected_scope=InviteScope.CLIENT
        )
    return register_and_accept(
        invite=invite, password=PASSWORD, expected_scope=InviteScope.CLIENT
    )[1]


def stored_contact(offer: ContactOffer) -> ClientContact:
    """Read the origin under the runtime tenant boundary."""
    with tenant_context(offer.firm.tenant.pk):
        return ClientContact.objects.get(pk=offer.contact.pk)


def assert_unaccepted(offer: ContactOffer) -> None:
    """A refusal must leave no link, acceptance timestamp, or target seat."""
    assert stored_contact(offer).membership_id is None
    assert Invite.objects.get(pk=offer.invite.pk).accepted_at is None
    assert not Membership.objects.filter(
        tenant=offer.firm.tenant, client_id=offer.contact.client_id
    ).exists()
