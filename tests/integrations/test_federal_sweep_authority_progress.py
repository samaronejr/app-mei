"""Missing authority consumes a bounded turn, never every later client's turn."""

import pytest

from apps.core.tenancy import tenant_context
from apps.integrations.authority import head_record, revoke_authority
from apps.integrations.models import IntegrationIssue
from tests.integrations.pagtoweb_support import World, page, world
from tests.integrations.test_federal_sweeps import (
    DAY,
    Fire,
    add_clients,
    fire,
    paid,
    states,
)

__all__ = ["fire", "world"]
pytestmark = pytest.mark.django_db(transaction=True)


def test_missing_authority_prefix_does_not_starve_authorized_clients(
    world: World, fire: Fire
) -> None:
    clients = [world.termo.client, *add_clients(world, 24, mei=True)]
    clients.sort(key=lambda client: client.pk)
    with tenant_context(world.termo.tenant.pk):
        for client in clients[:20]:
            head = head_record(client.pk, "pagtoweb")
            assert head is not None
            revoke_authority(
                world.termo.actor,
                client.pk,
                service="pagtoweb",
                expected_version=head.version,
            )
    world.script(*[page([]) for _ in range(5)])
    assert fire(DAY) == 0
    assert paid(world) == 0
    assert fire(DAY.replace(day=22)) == 5
    assert paid(world) == 5
    assert len(states(world)) == 5
    assert fire(DAY.replace(day=23)) == 0
    assert paid(world) == 5
    with tenant_context(world.termo.tenant.pk):
        assert set(
            IntegrationIssue.objects.filter(reason="autorizacao_ausente").values_list(
                "client_id", flat=True
            )
        ) == {client.pk for client in clients[:20]}
