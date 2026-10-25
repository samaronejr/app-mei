"""A7.4: included configuration and registration effects keep durable barriers."""

from dataclasses import replace
from typing import Literal
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.test import override_settings

from apps.integrations.call_contracts import (
    CallRequest,
    Reservation,
    StaleClaimError,
    operation_for,
    register_operation,
)
from apps.integrations.call_models import ProviderCall
from apps.integrations.call_protocol import claim, perform, reserve
from apps.integrations.models import ClientProviderLink
from tests.integrations.provider_peer import ProviderPeer
from tests.integrations.test_call_protocol import (
    SyntheticPolicy,
    provider_link,
    request,
)
from tests.integrations.test_provider_restore import restore_before_call

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["provider_link"]


class IncludedPolicy(SyntheticPolicy):
    def reserve(self, call: ProviderCall) -> Reservation:
        return Reservation(0, "synthetic-included-v1")


def effect_request(
    operation_class: Literal["registration", "configuration"],
) -> CallRequest:
    register_operation(
        replace(
            operation_for("SP-DAS"),
            code="SYN-EFFECT",
            operation_class=operation_class,
            billing_class="incluido_no_plano",
            policy=IncludedPolicy(),
        )
    )
    return replace(request(), operation="SYN-EFFECT")


@pytest.mark.parametrize("operation_class", ["registration", "configuration"])
def test_included_effect_cannot_start_again_under_a_new_request_key(
    provider_link: ClientProviderLink,
    operation_class: Literal["registration", "configuration"],
) -> None:
    # Given a completed included-plan effect.
    intent = effect_request(operation_class)
    row = reserve(provider_link.tenant_id, provider_link.pk, intent)
    assert row.reserved_microbrl == 0
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    with ProviderPeer() as peer:
        perform(ticket, peer)
        # When a caller changes only the request identity, not the business effect.
        second = reserve(
            provider_link.tenant_id,
            provider_link.pk,
            replace(
                intent,
                request_key=request("another").request_key,
            ),
        )
        repeated = claim(provider_link.tenant_id, second.pk)
        assert repeated is not None
        with pytest.raises(StaleClaimError):
            perform(repeated, peer)
        # Then the irreversible business barrier prevents a second transmission.
        assert peer.requests == [ticket.owner.hex]


@pytest.mark.parametrize("operation_class", ["registration", "configuration"])
def test_restore_rebuilds_included_effect_barrier_before_unpause(
    provider_link: ClientProviderLink,
    operation_class: Literal["registration", "configuration"],
) -> None:
    # Given a completed effect newer than the restored call and barrier tables.
    intent = effect_request(operation_class)
    row = reserve(provider_link.tenant_id, provider_link.pk, intent)
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    with ProviderPeer() as peer:
        perform(ticket, peer)
        restore_before_call()
        with override_settings(PROVIDERS_PAUSED=True):
            call_command("reconcile_provider_operations", restore_ref=str(uuid4()))
        # When a post-restore caller gives that same effect a different request key.
        second = reserve(
            provider_link.tenant_id,
            provider_link.pk,
            replace(
                intent,
                request_key=request("after-restore").request_key,
            ),
        )
        repeated = claim(provider_link.tenant_id, second.pk)
        assert repeated is not None
        with pytest.raises(StaleClaimError):
            perform(repeated, peer)
        # Then the restored barrier, not just the old call key, prevents a repeat.
        assert peer.requests == [ticket.owner.hex]
