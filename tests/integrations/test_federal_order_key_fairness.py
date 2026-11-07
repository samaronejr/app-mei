"""The candidate ordering must agree with the persisted cursor key."""

from uuid import UUID

import pytest
from django.db.models import QuerySet

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


@pytest.mark.parametrize("field", ["federal_payments_cursor", "federal_sitfis_cursor"])
def test_cursor_query_declares_pk_order_before_visiting_reverse_inserted_clients(
    lane: None, field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    [(tenant_id, _)] = firms(1, 0).items()
    ids = [UUID(int=50_000 + i) for i in range(41)]
    with tenant_context(tenant_id):
        for i, pk in reversed(list(enumerate(ids))):
            ClientCompany.objects.create(
                id=pk,
                tenant_id=tenant_id,
                legal_name=f"Heap ordering {i}",
                cnpj=cnpj(f"HP{i:04d}000001"),
            )
    visited: list[UUID] = []

    def record(client: ClientCompany) -> sweeps.Outcome:
        visited.append(client.pk)
        return sweeps.Outcome.REFUSED

    fetch_all = QuerySet._fetch_all
    checked: list[bool] = []

    def fetch_with_order_contract(queryset: QuerySet[ClientCompany]) -> None:
        if queryset.model is ClientCompany:
            assert queryset.ordered
            assert queryset.query.order_by == ("pk",)
            checked.append(True)
        fetch_all(queryset)

    # Inspect the real evaluated queryset, then let Django fetch its real rows.
    # This detects missing ordering even if a PK index happens to mask it.
    monkeypatch.setattr(QuerySet, "_fetch_all", fetch_with_order_contract)
    with tenant_context(tenant_id):
        current = None
        for _day in range(21, 32):
            expected = (
                ids
                if current is None
                else [pk for pk in ids if pk > current]
                + [pk for pk in ids if pk <= current]
            )[:20]
            before = len(visited)
            assert sweeps._turn(
                tenant_id,
                field,
                lambda: ClientCompany.objects.filter(pk__in=ids),
                record,
            ) == (0, 0)
            assert visited[before:] == expected
            current = expected[-1]
        assert set(visited) == set(ids)
        assert checked
