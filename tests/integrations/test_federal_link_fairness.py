"""Monthly fairness excludes inactive and unrelated provider links before slots."""

from datetime import date
from typing import Literal
from uuid import UUID

import pytest

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.integrations.models import ClientProviderLink, ProviderConnection
from apps.integrations.serpro.models import PaymentQuery
from tests.integrations.pagtoweb_support import World, page, world
from tests.integrations.test_federal_sweeps import Fire, add_clients, cnpj, fire, paid

__all__ = ["fire", "world"]
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("kind", ["revoked", "other-provider"])
def test_ineligible_link_prefix_cannot_exhaust_the_month(
    world: World, fire: Fire, kind: Literal["revoked", "other-provider"]
) -> None:
    eligible = [world.termo.client, *add_clients(world, 4, mei=True)]
    with tenant_context(world.termo.tenant.pk):
        connection = world.termo.connection
        status = "revogada"
        if kind == "other-provider":
            connection = ProviderConnection.objects.create(
                tenant=world.termo.tenant,
                provider="focus",
                environment="fake",
                status="ativa",
            )
            status = "ativa"
        for index in range(240):
            client = ClientCompany.objects.create(
                id=UUID(int=10_000 + index),
                tenant=world.termo.tenant,
                legal_name=f"Ineligible link synthetic client {index}",
                cnpj=cnpj(f"ZZ{index:04d}000001"),
                is_mei=True,
            )
            ClientProviderLink.objects.create(
                tenant=world.termo.tenant,
                client=client,
                connection=connection,
                provider_company_ref=f"ineligible-{index}",
                status=status,
            )
    assert UUID(int=10_239) < min(client.pk for client in eligible)
    world.script(*[page([]) for _ in eligible])
    results = [fire(date(2026, 10, day)) for day in range(21, 32)]
    with tenant_context(world.termo.tenant.pk):
        consulted = set(PaymentQuery.objects.values_list("client_id", flat=True))
    expected = {client.pk for client in eligible}
    assert consulted == expected, (
        "ineligible links must not starve eligible clients for the entire month"
    )
    assert results == [5, *[0 for _ in range(10)]]
    assert paid(world) == 5
