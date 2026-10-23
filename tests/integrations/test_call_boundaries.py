"""Independent witnesses for the protocol's scope, fence and storage boundaries."""

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.integrations.call_contracts import StaleClaimError
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_protocol import begin_io, claim, perform, reserve
from apps.integrations.journal_schema import JournalError, decode
from apps.integrations.models import ClientProviderLink
from tests.integrations.provider_peer import ProviderPeer
from tests.integrations.test_call_protocol import provider_link, reload_call, request

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["provider_link"]


def test_current_owner_with_old_fence_cannot_start(
    provider_link: ClientProviderLink,
) -> None:
    # Given the correct owner and epoch but an obsolete fence.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    with tenant_context(provider_link.tenant_id):
        ProviderCall.objects.filter(pk=row.pk).update(fence=1)
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    # When only the fence is stale.
    with ProviderPeer() as peer, pytest.raises(StaleClaimError):
        perform(replace(ticket, fence=ticket.fence - 1), peer)
    # Then the real claim remains unconsumed and the wire untouched.
    assert reload_call(provider_link, row.pk).state == CallState.CLAIMED
    assert peer.requests == []


def test_dispatch_itself_quarantines_an_expired_lease(
    provider_link: ClientProviderLink,
) -> None:
    # Given an expired lease not yet visited by another claim sweep.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    with tenant_context(provider_link.tenant_id):
        ProviderCall.objects.filter(pk=row.pk).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
    # When its original worker resumes directly at dispatch.
    with ProviderPeer() as peer, pytest.raises(StaleClaimError):
        perform(ticket, peer)
    # Then the refusal commits uncertainty instead of rolling it back with an error.
    assert reload_call(provider_link, row.pk).state == CallState.UNCERTAIN
    assert peer.requests == []


def test_journal_schema_refuses_another_tenant_even_with_valid_fields(
    provider_link: ClientProviderLink,
) -> None:
    # Given an otherwise valid committed receipt.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    receipt = begin_io(ticket).receipt
    # When its bytes appear under another tenant's scope.
    with pytest.raises(JournalError):
        decode(receipt.encode(), uuid4())
    # Then the owning scope remains a valid positive control.
    assert decode(receipt.encode(), provider_link.tenant_id) == receipt
