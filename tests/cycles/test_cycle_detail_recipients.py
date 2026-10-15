"""The display's batched seat and invitation answers must match live previews."""

from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.cycles.display_recipients import display_recipients
from apps.cycles.services.requests import recipients
from apps.tenants.models import Invite, Membership, TenantRole
from tests.portal.request_fixtures import portal_setup

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("invite_state", ["live", "expired", "accepted", "revoked"])
def test_batched_recipient_access_equals_live_seat_and_invite_predicates(
    monkeypatch: pytest.MonkeyPatch, invite_state: str
) -> None:
    # Given an address-matched seat, a direct seat and a seatless invited contact.
    moment = datetime(2026, 9, 20, 12, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: moment)
    world = portal_setup(f"t36-recipient-{invite_state}")
    with tenant_context(world.graph.tenant.pk):
        seat = Membership.objects.get(
            tenant=world.graph.tenant, client=world.graph.client, user=world.portal_user
        )
        direct = ClientContact.objects.create(
            tenant=world.graph.tenant,
            client=world.graph.client,
            name="Direct seat, different email",
            email="different@t36-recipient.test",
            membership=seat,
        )
        invited = ClientContact.objects.create(
            tenant=world.graph.tenant,
            client=world.graph.client,
            name="Invitation contact",
            email="INVITED@t36-recipient.test",
        )
        invitation, _ = Invite.issue(
            tenant=world.graph.tenant,
            client=world.graph.client,
            email=invited.email.lower(),
            role=TenantRole.CLIENT_OWNER,
            expires_at=moment - timedelta(days=1)
            if invite_state == "expired"
            else moment + timedelta(days=1),
        )
        Invite.objects.filter(pk=invitation.pk).update(
            accepted_at=moment if invite_state == "accepted" else None,
            revoked_at=moment if invite_state == "revoked" else None,
        )
        expected = {
            row.contact.pk: (row.access, row.invite.pk if row.invite else None)
            for row in recipients(world.graph.client)
        }
        # When the display query classifies the same contact set.
        actual = {
            row.contact.pk: (row.access, row.invite_id)
            for row in display_recipients(world.graph.client)
        }
    # Then case-insensitive addresses and direct seat precedence are preserved.
    assert actual == expected
    assert actual[world.contact.pk] == ("portal", None)
    assert actual[direct.pk] == ("portal", None)
    assert actual[invited.pk] == (
        ("invite", invitation.pk) if invite_state == "live" else ("ineligible", None)
    )
