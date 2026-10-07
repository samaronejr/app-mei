"""Measure the complete Hoje HTTP response, with real coverage-active clients."""

import pytest
from django.urls import reverse
from pytest_django.fixtures import DjangoAssertNumQueries

from apps.clients.models import Engagement, TaxProfile
from apps.core.tenancy import tenant_context
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
