"""Existing R1 invitation paths must not invent tenant notice consent."""

from http import HTTPStatus

import pytest
from django.core import mail
from django.urls import reverse

from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.delivery.models import OutboundMessage
from apps.delivery.notice_authority import configure, validate_notice
from apps.tenants.models import Invite, TenantRole
from tests.cycles.cycle_fixtures import CycleSetup
from tests.delivery import notice_fixtures
from tests.delivery.test_portal_invite_outbox import _contact, _firm

pytestmark = pytest.mark.django_db(transaction=True)
graph = notice_fixtures.graph


@pytest.mark.parametrize("door", ["detail", "contact"])
def test_r1_invitation_without_notice_authority_has_no_side_effects(
    graph: CycleSetup, door: str
) -> None:
    # Given a valid, authorized R1 issuer and contact, but no tenant notice consent.
    contact = _contact(graph)
    url = (
        reverse("portal-invite-issue", args=[graph.client.pk])
        if door == "detail"
        else reverse("client-contact-invite", args=[graph.client.pk, contact.pk])
    )
    data = (
        {"email": contact.email, "role": TenantRole.CLIENT_OWNER}
        if door == "detail"
        else {"convite-role": TenantRole.CLIENT_OWNER, "convite-version": 1}
    )
    mail.outbox.clear()

    # When the actual existing endpoint tries to issue its templated notice.
    response = _firm(graph).post(url, data)

    # Then authority refusal precedes credential and message creation.
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert mail.outbox == []
    assert not Invite.objects.filter(tenant=graph.tenant).exists()
    with tenant_context(graph.tenant.pk):
        assert not OutboundMessage.objects.exists()
        assert ClientContact.objects.count() == 1


@pytest.mark.parametrize("policy", ["missing", "unacknowledged", "detail_only"])
def test_import_only_contact_refuses_without_exact_acknowledged_policy(
    graph: CycleSetup, policy: str
) -> None:
    # Given the import door without an acknowledged exact contact invitation grant.
    graph.tenant.features = ["import_r1"]
    graph.tenant.save(update_fields=["features"])
    if policy != "missing":
        with tenant_context(graph.tenant.pk):
            configure(
                graph.actor,
                kinds=("portal_invite",) if policy == "detail_only" else (),
                acknowledged=policy == "detail_only",
                expected_version=0,
            )
    contact = _contact(graph)
    mail.outbox.clear()

    # When the actual contact invitation form is submitted.
    response = _firm(graph).post(
        reverse("client-contact-invite", args=[graph.client.pk, contact.pk]),
        {"convite-role": TenantRole.CLIENT_OWNER, "convite-version": 1},
    )

    # Then no direct mail callback or orphan live invitation can bypass policy.
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert mail.outbox == []
    assert not Invite.objects.filter(tenant=graph.tenant).exists()
    with tenant_context(graph.tenant.pk):
        assert not OutboundMessage.objects.exists()


def test_import_only_contact_queues_with_exact_acknowledged_policy(
    graph: CycleSetup,
) -> None:
    graph.tenant.features = ["import_r1"]
    graph.tenant.save(update_fields=["features"])
    contact = _contact(graph)
    with tenant_context(graph.tenant.pk):
        configure(
            graph.actor,
            kinds=("contact_portal_invite",),
            acknowledged=True,
            expected_version=0,
        )
    mail.outbox.clear()

    response = _firm(graph).post(
        reverse("client-contact-invite", args=[graph.client.pk, contact.pk]),
        {"convite-role": TenantRole.CLIENT_OWNER, "convite-version": 1},
    )

    assert response.status_code == HTTPStatus.FOUND
    assert mail.outbox == []
    with tenant_context(graph.tenant.pk):
        message = OutboundMessage.objects.get()
        assert message.kind == "contact_portal_invite"
        assert message.contact_id == contact.pk
        assert validate_notice(message) == (True, "authorized")
    assert message.invite_id is not None
    assert Invite.objects.get(pk=message.invite_id).contact_id == contact.pk
