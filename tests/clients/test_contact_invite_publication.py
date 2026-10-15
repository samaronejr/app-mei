"""The imported/contact-invited/refreshed/accepted path reaches the real builder."""

from collections.abc import Callable
from http import HTTPStatus

import pytest
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.cycles.effects.build import BuildError
from apps.cycles.services.approvals import propose
from apps.tenants.models import Membership, TenantRole
from tests.clients.contact_invite_fixtures import EMAIL
from tests.clients.contact_invite_journey import (
    accept_path,
    contact_journey,
    portal_session,
    publication_draft,
)
from tests.ui.factories import PASSWORD

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("existing", [False, True])
def test_import_contact_refreshed_acceptance_reaches_publication(
    settings: SettingsWrapper,
    existing: bool,
    record_property: Callable[[str, bool], None],
) -> None:
    journey = contact_journey(settings)
    portal = portal_session(journey, existing=existing)
    payload = (
        {}
        if existing
        else {
            "full_name": "Invited contact",
            "password1": PASSWORD,
            "password2": PASSWORD,
        }
    )
    first = portal.post(accept_path(journey.first_token), payload)
    assert first.status_code == HTTPStatus.GONE
    accepted = portal.post(accept_path(journey.refreshed_token), payload)
    assert accepted.status_code == HTTPStatus.FOUND
    user = User.objects.get(email=EMAIL)
    member = Membership.objects.get(
        user=user, tenant=journey.firm.tenant, client_id=journey.contact.client_id
    )
    assert member.is_active
    assert member.role == TenantRole.CLIENT_OWNER
    with tenant_context(journey.firm.tenant.pk):
        contact = ClientContact.objects.get(pk=journey.contact.pk)
        record_property("T33_active_seat", member.is_active)
        record_property("T33_contact_membership_null", contact.membership_id is None)
        proposal = propose(
            journey.firm.accountant,
            journey.cycle.pk,
            publication_draft(contact),
            expected_version=journey.cycle.version,
        )
        assert contact.membership_id == member.pk
        assert proposal.client_id == contact.client_id


def test_changed_origin_is_refused_on_the_real_acceptance_and_publication_path(
    settings: SettingsWrapper,
) -> None:
    journey = contact_journey(settings)
    portal = portal_session(journey, existing=True)
    editor = journey.firm.sign_in(journey.firm.owner, totp_step=1)
    changed = editor.post(
        reverse(
            "client-contact-edit",
            args=[journey.contact.client_id, journey.contact.pk],
        ),
        {
            "contato-name": "Invited contact",
            "contato-email": EMAIL,
            "contato-channel_email_allowed": "on",
            "contato-version": 1,
        },
    )
    assert changed.status_code == HTTPStatus.FOUND
    refused = portal.post(accept_path(journey.refreshed_token), {})
    assert refused.status_code == HTTPStatus.NOT_FOUND
    assert not Membership.objects.filter(
        user__email=EMAIL,
        tenant=journey.firm.tenant,
        client_id=journey.contact.client_id,
    ).exists()
    with tenant_context(journey.firm.tenant.pk):
        contact = ClientContact.objects.get(pk=journey.contact.pk)
        assert contact.membership_id is None
        with pytest.raises(BuildError, match="recipient_ineligible"):
            propose(
                journey.firm.accountant,
                journey.cycle.pk,
                publication_draft(contact),
                expected_version=journey.cycle.version,
            )
