"""The candidate ordering must agree with the persisted cursor key."""

from uuid import UUID

import pytest

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.integrations.models import ClientProviderLink, ProviderConnection
from apps.integrations.serpro import sweeps
from tests.integrations.test_federal_sweeps import cnpj, firms, lane

__all__ = ["lane"]
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("field", ["federal_payments_cursor", "federal_sitfis_cursor"])
def test_reverse_cnpj_order_cannot_keep_an_eligible_prefix_unserved(
    lane: None, field: str
) -> None:
    [(tenant_id, _)] = firms(1, 0).items()
    ids = [UUID(int=10_000 + i) for i in range(41)]
    with tenant_context(tenant_id):
        provider = ProviderConnection.objects.get()
        for i, pk in enumerate(ids):
            client = ClientCompany.objects.create(
                id=pk,
                tenant_id=tenant_id,
                legal_name=f"Independent ordering {i}",
                cnpj=cnpj(f"OR{40 - i:04d}000001"),
            )
            ClientProviderLink.objects.create(
                tenant_id=tenant_id,
                client=client,
                connection=provider,
                provider_company_ref=f"order-{i}",
                status="ativa",
            )
    visited: list[UUID] = []

    def record(client: ClientCompany) -> sweeps.Outcome:
        visited.append(client.pk)
        return sweeps.Outcome.REFUSED

    for _day in range(21, 32):
        before = len(visited)
        assert sweeps._turn(tenant_id, field, sweeps._linked, record) == (0, 0)
        assert len(visited) - before == 20
    assert set(visited) == set(ids), (
        "every eligible client must receive a turn during the monthly window"
    )
