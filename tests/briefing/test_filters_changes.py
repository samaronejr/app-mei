"""Portfolio filters, personal queues and changes all narrow current facts."""

from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.briefing.queries import briefing
from apps.briefing.types import BriefingFilters
from apps.clients.models import ClientAssignment, ClientCompany, Engagement
from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleEvent, ServiceCycle
from tests.briefing.fixtures import NOW, briefing_setup, due_obligation
from tests.briefing.provider_fixtures import provider_graph, second_client

pytestmark = pytest.mark.django_db(transaction=True)
ALL_KINDS = {
    "obligation",
    "request",
    "exception",
    "approval",
    "delivery",
    "document",
    "coverage",
}


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        (BriefingFilters(service="mei_mensal"), ALL_KINDS),
        (BriefingFilters(service="documentos_mensal"), set()),
        (BriefingFilters(regime="mei"), ALL_KINDS),
        (BriefingFilters(regime="simples"), set()),
        (BriefingFilters(uf="SP"), ALL_KINDS),
        (BriefingFilters(uf="RJ"), set()),
        (BriefingFilters(municipality="3550308"), ALL_KINDS),
        (BriefingFilters(municipality="3304557"), set()),
        (BriefingFilters(period=date(2026, 9, 1)), ALL_KINDS),
        (BriefingFilters(period=date(2026, 10, 1)), set()),
        (BriefingFilters(work_state="due"), {"obligation"}),
        (BriefingFilters(risk_reason="request_overdue"), {"request"}),
        (BriefingFilters(freshness="stale"), {"coverage"}),
    ],
)
def test_m1_filters_narrow_items_and_the_same_counts(
    filters: BriefingFilters, expected: set[str]
) -> None:
    setup = briefing_setup()
    provider_graph(setup)
    with tenant_context(setup.tenant.pk):
        ClientCompany.objects.filter(pk=setup.client.pk).update(
            state="SP", municipality_ibge_code="3550308"
        )
        control = briefing(setup.actor, scope="team", filters=BriefingFilters())
        assert {item.kind for item in control.items} == ALL_KINDS

        page = briefing(setup.actor, scope="team", filters=filters)

    assert {item.kind for item in page.items} == expected
    assert {count.kind for count in page.counts if count.total} == expected
    assert sum(count.total for count in page.counts) == page.total == len(expected)


def test_personal_queue_and_responsible_filter_follow_the_live_owner() -> None:
    setup = briefing_setup()
    graph = provider_graph(setup)
    with tenant_context(setup.tenant.pk):
        ClientAssignment.objects.create(
            tenant=setup.tenant, client=setup.client, user=setup.staff
        )
        assert briefing(setup.staff, scope="team", filters=BriefingFilters()).total == 7
        assert briefing(setup.staff, scope="me", filters=BriefingFilters()).total == 0
        ServiceCycle.objects.filter(pk=graph.cycle.pk).update(owner=setup.staff)
        Engagement.objects.filter(pk=setup.engagement.pk).update(
            responsible=setup.staff
        )

        mine = briefing(setup.staff, scope="me", filters=BriefingFilters())
        selected = briefing(
            setup.actor,
            scope="team",
            filters=BriefingFilters(responsible=setup.staff.pk),
        )

    assert {item.kind for item in mine.items} == {
        "obligation",
        "request",
        "approval",
        "delivery",
        "document",
        "coverage",
    }
    assert mine.items == selected.items
    assert mine.total == selected.total == 6


def test_cursor_cannot_be_reused_for_another_selection_or_actor() -> None:
    setup = briefing_setup()
    due_obligation(setup, period=date(2026, 8, 1))
    due_obligation(setup)
    with tenant_context(setup.tenant.pk):
        first = briefing(setup.actor, scope="team", filters=BriefingFilters(), limit=1)
        assert first.next_cursor
        with pytest.raises(ValidationError):
            briefing(
                setup.actor,
                scope="team",
                filters=BriefingFilters(uf="SP"),
                cursor=first.next_cursor,
            )
        with pytest.raises(ValidationError):
            briefing(
                setup.staff,
                scope="team",
                filters=BriefingFilters(),
                cursor=first.next_cursor,
            )


def test_yesterday_changes_use_the_injected_clock_and_visible_clients() -> None:
    setup = briefing_setup()
    own = provider_graph(setup)
    other = provider_graph(second_client(setup))
    with tenant_context(setup.tenant.pk):
        ClientAssignment.objects.create(
            tenant=setup.tenant, client=setup.client, user=setup.staff
        )
        for graph, moment in [
            (own, NOW - timedelta(days=2)),
            (own, NOW - timedelta(days=1)),
            (own, NOW + timedelta(seconds=1)),
            (other, NOW),
        ]:
            CycleEvent.objects.create(
                tenant=setup.tenant,
                client_id=graph.cycle.client_id,
                cycle=graph.cycle,
                actor=setup.actor,
                type="document.reviewed",
                aggregate_type="EvidenceRequest",
                aggregate_id=graph.request.pk,
                aggregate_version=graph.request.version,
                occurred_at=moment,
                observed_at=moment,
            )
        control = briefing(setup.actor, scope="team", filters=BriefingFilters())
        assert len(control.changes) == 2

        page = briefing(setup.staff, scope="team", filters=BriefingFilters())

    assert len(page.changes) == 1
    assert page.changes[0].occurred_at == NOW - timedelta(days=1)
    assert page.changes[0].url == reverse("cycle-detail", args=[own.cycle.pk])
