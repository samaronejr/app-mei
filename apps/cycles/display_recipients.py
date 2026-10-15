"""Batch contact access for display only; dispatch still reads live authority."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict
from uuid import UUID

from django.db.models import Exists, OuterRef, Subquery
from django.utils import timezone

from apps.clients.models import ClientContact
from apps.tenants.models import Invite, Membership

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django_stubs_ext import WithAnnotations

    from apps.clients.models import ClientCompany


class ContactAccess(TypedDict):
    """Access facts evaluated inside the same scoped contact query."""

    preview_seated: bool
    preview_invite_id: UUID | None


@dataclass(frozen=True, slots=True)
class RecipientPreview:
    """A contact's displayed access, never an authorization to enqueue a notice."""

    contact: ClientContact
    access: str
    invite_id: UUID | None

    @property
    def eligible(self) -> bool:
        """Report the displayed eligibility, not dispatch authority."""
        return self.access != "ineligible"


def display_recipients(client: "ClientCompany") -> tuple[RecipientPreview, ...]:
    """Classify contacts with the seat and invitation predicates used by requests."""
    # PLATFORM_QUERY_OK: tenancy roots, explicitly confined to this firm/client.
    seats = Membership.objects.filter(
        tenant_id=client.tenant_id, client_id=client.pk, is_active=True
    )
    # PLATFORM_QUERY_OK: same firm/client and newest live invitation by address.
    invitations = Invite.objects.filter(
        tenant_id=client.tenant_id,
        client_id=client.pk,
        email__iexact=OuterRef("email"),
        accepted_at__isnull=True,
        revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).order_by("-created_at")
    contacts: QuerySet[WithAnnotations[ClientContact, ContactAccess]] = (
        ClientContact.objects.filter(
            client=client, active=True, channel_email_allowed=True
        )
        .exclude(email="")
        .annotate(
            preview_seated=Exists(seats.filter(pk=OuterRef("membership_id")))
            | Exists(seats.filter(user__email__iexact=OuterRef("email"))),
            preview_invite_id=Subquery(invitations.values("pk")[:1]),
        )
        .order_by("name", "pk")
    )
    return tuple(
        RecipientPreview(
            contact,
            "portal"
            if contact.preview_seated
            else "invite"
            if contact.preview_invite_id is not None
            else "ineligible",
            None if contact.preview_seated else contact.preview_invite_id,
        )
        for contact in contacts
    )
