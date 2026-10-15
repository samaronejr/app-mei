"""The T48 guard extends T53 without backfilling or rewriting notice authority."""

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client

from apps.clients.maintenance import invite_contact
from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.delivery.models import OutboundMessage
from apps.delivery.notice_authority import (
    NoticeOrigin,
    lock_notice_scope,
    validate_notice,
)
from apps.delivery.outbox import enqueue_portal_invite
from apps.tenants.models import Invite, TenantRole
from tests.delivery import test_contact_invite_flags
from tests.delivery.test_contact_invite_flags import _post, _rows
from tests.ui.factories import Firm

pytestmark = pytest.mark.django_db(transaction=True)
contact_origin = test_contact_invite_flags.contact_origin


def _queue_contact_invitation(firm: Firm, contact: ClientContact) -> None:
    """Issue and enqueue in one transaction, as the contact invitation view does."""
    with transaction.atomic():
        scope = lock_notice_scope(
            firm.owner,
            NoticeOrigin("contact_portal_invite", contact.client_id),
        )
        invite, _token = invite_contact(
            actor=scope.actor,
            tenant=firm.tenant,
            client_id=contact.client_id,
            contact_id=contact.pk,
            role=TenantRole.CLIENT_OWNER,
            expected_version=contact.version,
        )
        enqueue_portal_invite(
            scope.actor,
            invite,
            contact,
            kind="contact_portal_invite",
            notice_scope=scope,
        )


def test_contact_origin_guard_roundtrip_retains_binding_and_refusals(
    contact_origin: tuple[Firm, ClientContact, Client],
) -> None:
    firm, contact, web = contact_origin
    assert _post(web, contact) == 302
    [message] = _rows(firm)
    original_bytes = message.notice_authorization_bytes
    with tenant_context(firm.tenant.pk):
        second = ClientContact.objects.create(
            tenant=firm.tenant,
            client_id=contact.client_id,
            name="Second contact",
            email="second@t48.example.test",
        )
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        MigrationExecutor(connection).migrate(
            [("delivery", "0006_notice_authorization")]
        )
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")
        # Real current authority bytes are minted by the API. The T53-only SQL
        # guard must still reject this new origin, not silently accept any kind.
        with tenant_context(firm.tenant.pk), pytest.raises(IntegrityError) as error:
            _queue_contact_invitation(firm, second)
        assert getattr(error.value.__cause__, "sqlstate", None) == "23514"
        # PLATFORM_QUERY_OK: the refused unit leaves no live invitation behind.
        assert not Invite.objects.filter(tenant=firm.tenant, contact=second).exists()
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
        try:
            MigrationExecutor(connection).migrate(leaves)
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")

    assert _post(web, second) == 302
    with tenant_context(firm.tenant.pk):
        message.refresh_from_db()
        assert message.notice_authorization_bytes == original_bytes
        assert validate_notice(message) == (True, "authorized")
        assert OutboundMessage.objects.count() == 2
        # Existing T53 immutability still applies to the new contact kind.
        with pytest.raises(IntegrityError) as error, transaction.atomic():
            OutboundMessage.objects.filter(pk=message.pk).update(kind="portal_invite")
        assert getattr(error.value.__cause__, "sqlstate", None) == "23514"
        with pytest.raises(IntegrityError) as error, transaction.atomic():
            OutboundMessage.objects.filter(pk=message.pk).update(
                notice_authorization_bytes=None, notice_authorization_digest=None
            )
        assert getattr(error.value.__cause__, "sqlstate", None) == "23514"
