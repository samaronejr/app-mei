"""A failed read must be named, not reported as a reassuring zero."""

from datetime import date, datetime

import pytest
from django.db import connection
from django.urls import reverse

from apps.accounts.models import User
from apps.briefing import providers, queries
from apps.briefing.operation_sources import deliveries, obligations
from apps.briefing.source_rows import SourceContext, SourceQuery
from apps.briefing.types import BriefingFilters, BriefingItem
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from tests.briefing.fixtures import briefing_setup
from tests.briefing.http_fixtures import http_firm
from tests.briefing.provider_fixtures import provider_graph

pytestmark = pytest.mark.django_db(transaction=True)


def _broken_delivery(context: SourceContext) -> SourceQuery:
    return deliveries(context).extra(where=["1 / 0 = 1"])


def _broken_query(context: SourceContext) -> SourceQuery:
    return obligations(context).extra(where=["1 / 0 = 1"])


def _broken_coverage(
    actor: User, clients: list[ClientCompany], period: date, *, now: datetime
) -> list[BriefingItem]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 / 0")
    return []


def test_one_failed_provider_preserves_other_scoped_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = briefing_setup()
    graph = provider_graph(setup)
    monkeypatch.setitem(providers.PROVIDERS, "delivery", _broken_delivery)
    with tenant_context(setup.tenant.pk):
        page = queries.briefing(setup.actor, scope="team", filters=BriefingFilters())

    assert page.failed_providers == ("delivery",)
    assert {item.kind for item in page.items} == set(graph.subjects) - {"delivery"}
    assert page.total == 6
    assert "delivery" not in {count.kind for count in page.counts}


def test_coverage_failure_preserves_the_other_six_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = briefing_setup()
    provider_graph(setup)
    monkeypatch.setattr(queries, "COVERAGE_PROVIDER", _broken_coverage)
    with tenant_context(setup.tenant.pk):
        page = queries.briefing(setup.actor, scope="team", filters=BriefingFilters())

    assert page.failed_providers == ("coverage",)
    assert page.total == 6
    assert all(item.kind != "coverage" for item in page.items)


def test_all_failed_providers_render_retry_instead_of_empty_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firm = http_firm()
    for kind in providers.PROVIDERS:
        monkeypatch.setitem(providers.PROVIDERS, kind, _broken_query)
    monkeypatch.setattr(queries, "COVERAGE_PROVIDER", _broken_coverage)

    response = firm.as_owner().get(reverse("dashboard"))

    assert response.status_code == 503
    assert response.context["page"].failed_providers == tuple(providers.PROVIDER_LABELS)
    assert b'id="briefing-unavailable"' in response.content
    assert b'data-count="0"' not in response.content


def test_unassigned_portfolio_does_not_invoke_a_failing_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = briefing_setup()
    provider_graph(setup)
    monkeypatch.setattr(queries, "COVERAGE_PROVIDER", _broken_coverage)
    with tenant_context(setup.tenant.pk):
        page = queries.briefing(setup.staff, scope="team", filters=BriefingFilters())

    assert page.items == ()
    assert page.failed_providers == ()
    assert page.total == 0
