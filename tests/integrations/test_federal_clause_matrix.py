"""Selection clauses protect slots before admission, across the month's turns."""

from datetime import date
from uuid import UUID

import pytest
from django.core.exceptions import MultipleObjectsReturned
from django.db import IntegrityError
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.delivery.models import AutomationPause
from apps.integrations.models import ClientProviderLink, ProviderConnection
from apps.integrations.serpro import sweeps
from apps.integrations.serpro.pagtoweb import PaymentRange
from tests.integrations.test_federal_sweeps import cnpj, firms, lane

__all__ = ["lane"]
pytestmark = pytest.mark.django_db(transaction=True)
WINDOW = PaymentRange(date(2026, 9, 1), date(2026, 10, 21))


@pytest.mark.parametrize(
    "clause",
    ["provider", "environment", "link-status", "unlinked", "suspended", "closed"],
)
@pytest.mark.parametrize("field", ["federal_payments_cursor", "federal_sitfis_cursor"])
def test_one_link_clause_cannot_spend_another_clients_slots(
    lane: None, clause: str, field: str
) -> None:
    made = firms(2, 25)
    for firm, (tenant_id, eligible) in enumerate(made.items()):
        with tenant_context(tenant_id):
            connection = ProviderConnection.objects.get()
            if clause in {"provider", "environment"}:
                connection = ProviderConnection.objects.create(
                    tenant_id=tenant_id,
                    provider="focus" if clause == "provider" else "serpro",
                    environment="homologacao" if clause == "environment" else "fake",
                    status="ativa",
                )
            for i in range(30):
                client = ClientCompany.objects.create(
                    id=UUID(int=1000 + firm * 100 + i),
                    tenant_id=tenant_id,
                    legal_name=f"One-clause prefix {clause} {i}",
                    cnpj=cnpj(f"ZZ{i:04d}000001"),
                    status=clause if clause in {"suspended", "closed"} else "active",
                )
                if clause != "unlinked":
                    ClientProviderLink.objects.create(
                        tenant_id=tenant_id,
                        client=client,
                        connection=connection,
                        provider_company_ref=f"prefix-{i}",
                        status="revogada" if clause == "link-status" else "ativa",
                    )
        visits: list[UUID] = []

        def record(
            client: ClientCompany, visited: list[UUID] = visits
        ) -> sweeps.Outcome:
            visited.append(client.pk)
            return sweeps.Outcome.QUEUED

        for _day in range(21, 32):
            before = len(visits)
            sweeps._turn(tenant_id, field, sweeps._linked, record)
            assert 0 < len(visits) - before <= 20
            assert set(visits) <= set(eligible)
        assert set(visits) == set(eligible)


@pytest.mark.parametrize("field", ["federal_payments_cursor", "federal_sitfis_cursor"])
@pytest.mark.parametrize("count", [0, 1, 19, 20, 21, 39, 40, 41])
@pytest.mark.parametrize("position", ["none", "first", "middle", "last", "gap"])
def test_cursor_order_bounds_and_remaining_slots(
    lane: None, field: str, count: int, position: str
) -> None:
    [(tenant_id, _)] = firms(1, 0).items()
    ids = [UUID(int=10_000 + 2 * i) for i in range(count)]
    with tenant_context(tenant_id):
        connection = ProviderConnection.objects.get()
        # Heap/insertion order deliberately disagrees with the promised PK order.
        for i, pk in reversed(list(enumerate(ids))):
            client = ClientCompany.objects.create(
                id=pk,
                tenant_id=tenant_id,
                legal_name=f"Cursor {i}",
                cnpj=cnpj(f"CU{i:04d}000001"),
            )
            ClientProviderLink.objects.create(
                tenant_id=tenant_id,
                client=client,
                connection=connection,
                provider_company_ref=f"cursor-{i}",
                status="ativa",
            )
        cursor = None
        if ids and position != "none":
            cursor = {
                "first": ids[0],
                "middle": ids[len(ids) // 2],
                "last": ids[-1],
                "gap": UUID(int=10_001),
            }[position]
        control = AutomationPause.objects.create(tenant_id=tenant_id, **{field: cursor})
        untouched = (
            control.federal_sitfis_cursor
            if field == "federal_payments_cursor"
            else control.federal_payments_cursor
        )
    visits: list[UUID] = []
    error = None
    for _day in range(21, 32):
        expected = (
            ids
            if cursor is None
            else (
                [pk for pk in ids if pk > cursor] + [pk for pk in ids if pk <= cursor]
            )
        )
        expected = expected[:20]
        before = len(visits)

        def record(client: ClientCompany) -> sweeps.Outcome:
            visits.append(client.pk)
            return sweeps.Outcome.QUEUED

        try:
            queued, failed = sweeps._turn(tenant_id, field, sweeps._linked, record)
        except (ValueError, IntegrityError, MultipleObjectsReturned) as caught:
            error = caught
        assert error is None, f"a valid cursor turn must complete: {error}"
        assert visits[before:] == expected
        assert (queued, failed) == (len(expected), 0)
        cursor = expected[-1] if expected else cursor
        with tenant_context(tenant_id):
            control.refresh_from_db()
            assert getattr(control, field) == cursor
            assert (
                control.federal_sitfis_cursor
                if field == "federal_payments_cursor"
                else control.federal_payments_cursor
            ) == untouched
    assert set(visits) == set(ids)


@pytest.mark.parametrize("field", ["federal_payments_cursor", "federal_sitfis_cursor"])
def test_a_turn_does_not_advance_a_replacement_control_it_did_not_claim(
    lane: None, field: str
) -> None:
    [(tenant_id, _)] = firms(1, 1).items()
    sentinel = UUID(int=42)
    with tenant_context(tenant_id):
        original = AutomationPause.objects.create(tenant_id=tenant_id)

    def replace_control(client: ClientCompany) -> sweeps.Outcome:
        AutomationPause.objects.filter(pk=original.pk).delete()
        AutomationPause.objects.create(tenant_id=tenant_id, **{field: sentinel})
        return sweeps.Outcome.QUEUED

    assert sweeps._turn(tenant_id, field, sweeps._linked, replace_control) == (1, 0)
    with tenant_context(tenant_id):
        replacement = AutomationPause.objects.get()
        assert replacement.pk != original.pk
        assert getattr(replacement, field) == sentinel
        assert (
            replacement.federal_sitfis_cursor
            if field == "federal_payments_cursor"
            else replacement.federal_payments_cursor
        ) is None


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("environment", ["fake", "homologacao", "producao"])
@pytest.mark.parametrize(
    ("platform", "serpro"),
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_global_guard_truth_table(
    settings: SettingsWrapper,
    approved: bool,
    environment: str,
    platform: bool,
    serpro: bool,
) -> None:
    settings.PROVIDERS_PAUSED = platform
    settings.SERPRO_PAUSED = serpro
    settings.SERPRO_ENVIRONMENT = environment
    settings.PROVIDER_REAL_DATA_APPROVED = approved
    expected = ""
    if platform or serpro:
        expected = "pausado"
    elif environment == "producao" and not approved:
        expected = "ambiente_nao_aprovado"
    assert sweeps.global_refusal() == expected
