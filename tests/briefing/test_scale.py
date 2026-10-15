"""Measure the complete Hoje HTTP response, with real coverage-active clients."""

import pytest
from django.urls import reverse
from pytest_django.fixtures import DjangoAssertNumQueries

from apps.clients.models import Engagement, TaxProfile
from apps.core.tenancy import tenant_context
from apps.cycles.models import PlaybookVersion
from apps.cycles.packs import install_system_packs
from apps.obligations.coverage import CoverageState, coverage_for_clients
from apps.obligations.models import Obligation, ObservationVerificationStatus
from apps.obligations.models.observations import ObservationOutcome
from apps.obligations.observations import assert_fact
from tests.briefing.fixtures import NOW
from tests.briefing.provider_fixtures import provider_graph
from tests.cycles.cycle_fixtures import PERIOD, CycleSetup
from tests.factories.scale import build_firm


@pytest.mark.scale
@pytest.mark.django_db(transaction=True)
def test_hoje_has_a_25_query_budget_for_one_thousand_clients(
    django_assert_max_num_queries: DjangoAssertNumQueries,
) -> None:
    firm = build_firm(1_000, seed=42)
    with tenant_context(firm.tenant.pk):
        assert Engagement.objects.count() == 1_000
        assert TaxProfile.objects.count() == 1_000
    session = firm.as_owner()

    with django_assert_max_num_queries(25):
        response = session.get(reverse("dashboard"))

    assert response.status_code == 200
    assert response.context["page"].total == 1_000
    assert len(response.context["page"].items) == 50


@pytest.mark.scale
@pytest.mark.django_db(transaction=True)
def test_hoje_keeps_the_budget_when_every_provider_has_items(
    django_assert_max_num_queries: DjangoAssertNumQueries,
) -> None:
    firm = build_firm(1_000, seed=43)
    stale_client, failed_client = firm.clients[:2]
    with tenant_context(firm.tenant.pk):
        install_system_packs(firm.tenant)
        setup = CycleSetup(
            firm.tenant,
            firm.owner,
            firm.accountant,
            stale_client,
            Engagement.objects.get(client=stale_client),
            PlaybookVersion.objects.get(playbook__key="mei-mensal", state="published"),
        )
        provider_graph(setup, obligation=Obligation.objects.get(client=stale_client))
        failed = Obligation.objects.get(client=failed_client)
        assert_fact(
            tenant_id=firm.tenant.pk,
            client_id=failed_client.pk,
            subject_id=failed.pk,
            kind="payment",
            period=PERIOD,
            source_kind="official_api",
            observed_at=NOW,
            value={},
            verification_status=ObservationVerificationStatus.REFRESH_FAILED,
            outcome=ObservationOutcome.RECORDED,
        )
        coverage = coverage_for_clients([stale_client, failed_client], PERIOD, now=NOW)
        assert [answer.state for answer in coverage[stale_client.pk]] == [
            CoverageState.STALE
        ]
        assert [answer.state for answer in coverage[failed_client.pk]] == [
            CoverageState.REFRESH_FAILED
        ]
    session = firm.as_owner()

    with django_assert_max_num_queries(25):
        response = session.get(reverse("dashboard"))

    assert response.status_code == 200
    page = response.context["page"]
    assert {count.kind: count.total for count in page.counts} == {
        "obligation": 1_000,
        "request": 1,
        "exception": 1,
        "approval": 1,
        "delivery": 1,
        "document": 1,
        "coverage": 2,
    }
    assert page.total == 1_007
    assert page.failed_providers == ()
    assert len(page.items) == 50
