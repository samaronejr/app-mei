"""One coverage policy, fixed portfolio query cost, and no widened read set."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from django.db import models
from django.db.models.signals import post_init
from pytest_django.fixtures import DjangoAssertNumQueries

from apps.authz.portfolio import visible_clients
from apps.clients.models import ClientCompany, Engagement, TaxProfile, TaxRegime
from apps.core.flags import open_flag_cache, prime_flag_cache
from apps.core.tenancy import tenant_context
from apps.obligations.coverage import CoverageState, coverage_for, coverage_for_clients
from apps.obligations.models import Obligation, SourceObservation
from apps.obligations.observations import emit_calendar_assertions
from tests.cycles.cycle_fixtures import PERIOD, CycleSetup, cycle_setup
from tests.cycles.observation_fixtures import payment_claim
from tests.ui.factories import make_cnpj

pytestmark = pytest.mark.django_db(transaction=True)
CLOCK = datetime(2026, 10, 7, 12, tzinfo=UTC)
STATES = (
    CoverageState.UNKNOWN,
    CoverageState.NOT_APPLICABLE,
    CoverageState.MANUAL,
    CoverageState.STALE,
    CoverageState.VERIFIED,
)


@pytest.fixture
def portfolio() -> tuple[CycleSetup, list[ClientCompany], dict[UUID, str]]:
    graph = cycle_setup("t17-bulk-coverage")
    clients = [graph.client]
    expected = {}
    with tenant_context(graph.tenant.pk):
        for index in range(1, 20):
            client = ClientCompany.objects.create(
                tenant=graph.tenant,
                legal_name=f"Coverage client {index:02}",
                cnpj=make_cnpj(f"{index:012}"),
            )
            clients.append(client)
            Engagement.objects.create(
                tenant=graph.tenant,
                client=client,
                service_kind="mei_mensal",
                starts_on=PERIOD,
                provenance="staff_entry",
            )
            TaxProfile.objects.create(
                tenant=graph.tenant,
                client=client,
                regime=TaxRegime.SIMPLES if index % 5 == 1 else TaxRegime.MEI,
                valid_from=PERIOD,
                provenance="staff_entry",
            )
        for index, client in enumerate(clients):
            expected[client.pk] = STATES[index % len(STATES)]
            subject = Obligation.objects.create(
                tenant=graph.tenant,
                client=client,
                obligation_type_id="DAS",
                competence_month=PERIOD,
                nominal_due_date=PERIOD.replace(month=10, day=20),
                resolved_due_date=PERIOD.replace(month=10, day=20),
            )
            assert emit_calendar_assertions([subject]) == 1
            if index % 5 == 2:
                payment_claim(subject, observed_at=CLOCK - timedelta(days=1))
                payment_claim(
                    subject,
                    source="official_api",
                    verification="refresh_failed",
                    outcome="refresh_failed",
                    observed_at=CLOCK,
                )
            elif index % 5 == 3:
                payment_claim(
                    subject,
                    source="official_api",
                    verification="verified",
                    outcome="accepted",
                    observed_at=CLOCK - timedelta(days=7),
                )
            elif index % 5 == 4:
                payment_claim(
                    subject,
                    source="official_file_import",
                    verification="verified",
                    outcome="accepted",
                    observed_at=CLOCK - timedelta(days=90),
                )
        scoped = list(
            visible_clients(graph.actor, tenant_id=graph.tenant.pk).filter(
                pk__in=[client.pk for client in clients]
            )
        )
    assert {client.pk for client in scoped} == set(expected)
    return graph, scoped, expected


def test_populated_bulk_equals_single_client_coverage(
    portfolio: tuple[CycleSetup, list[ClientCompany], dict[UUID, str]],
) -> None:
    graph, clients, states = portfolio
    with tenant_context(graph.tenant.pk):
        bulk = coverage_for_clients(iter(clients), PERIOD, now=CLOCK)
        single = {
            client.pk: coverage_for(graph.actor, client, PERIOD, now=CLOCK)
            for client in clients
        }
    assert bulk == single
    assert {client_id: items[0].state for client_id, items in bulk.items()} == states
    for client_id, items in bulk.items():
        item = items[0]
        if states[client_id] == CoverageState.MANUAL:
            assert item.head_source == "staff_recorded"
            assert item.last_success == CLOCK - timedelta(days=1)
            assert item.covered_by["payment"].pk == item.head_id
        elif states[client_id] == CoverageState.VERIFIED:
            assert item.last_success == CLOCK - timedelta(days=90)
            assert item.head_source == "official_file_import"


def test_bulk_query_count_is_constant_for_one_and_twenty(
    portfolio: tuple[CycleSetup, list[ClientCompany], dict[UUID, str]],
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    graph, clients, _states = portfolio
    with tenant_context(graph.tenant.pk):
        for selected in (clients[:1], clients):
            # Outside a request: one entitlement read plus four data batches.
            with django_assert_num_queries(5):
                result = coverage_for_clients(selected, PERIOD, now=CLOCK)
                assert len(result) == len(selected)
        with open_flag_cache():
            prime_flag_cache(graph.tenant)
            for selected in (clients[:1], clients):
                # TenantMiddleware already primes this in T19's real request.
                with django_assert_num_queries(4):
                    result = coverage_for_clients(selected, PERIOD, now=CLOCK)
                    assert len(result) == len(selected)


def test_clients_outside_the_passed_set_are_never_read(
    portfolio: tuple[CycleSetup, list[ClientCompany], dict[UUID, str]],
) -> None:
    graph, clients, _states = portfolio
    selected = clients[:2]
    watched = (Engagement, TaxProfile, Obligation, SourceObservation)
    seen: dict[str, set[UUID]] = {model._meta.db_table: set() for model in watched}

    def observe(
        sender: type[models.Model],
        instance: Engagement | TaxProfile | Obligation | SourceObservation,
        **kwargs: object,
    ) -> None:
        seen[sender._meta.db_table].add(instance.client_id)

    for model in watched:
        post_init.connect(observe, sender=model, weak=False)
    try:
        with tenant_context(graph.tenant.pk):
            result = coverage_for_clients(selected, PERIOD, now=CLOCK)
    finally:
        for model in watched:
            post_init.disconnect(observe, sender=model)
    included = {client.pk for client in selected}
    assert set(result) == included
    assert all(read_ids == included for read_ids in seen.values()), seen


@pytest.mark.parametrize(
    ("offset", "state"),
    [(-1, CoverageState.VERIFIED), (0, CoverageState.STALE), (1, CoverageState.STALE)],
)
def test_bulk_uses_the_same_half_open_api_freshness(
    portfolio: tuple[CycleSetup, list[ClientCompany], dict[UUID, str]],
    offset: int,
    state: str,
) -> None:
    graph, clients, states = portfolio
    with tenant_context(graph.tenant.pk):
        result = coverage_for_clients(
            clients, PERIOD, now=CLOCK + timedelta(microseconds=offset)
        )
    for client_id, initial in states.items():
        if initial == CoverageState.STALE:
            assert result[client_id][0].state == state


def test_empty_bulk_needs_no_queries(
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    with django_assert_num_queries(0):
        assert coverage_for_clients([], PERIOD, now=CLOCK) == {}
