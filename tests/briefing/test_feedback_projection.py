"""Feedback data is personal even when two people share the same portfolio."""

from datetime import timedelta

import pytest

from apps.briefing.feedback import Dismissal, dismiss
from apps.briefing.queries import briefing
from apps.briefing.types import BriefingFilters
from apps.clients.models import ClientAssignment
from apps.core.tenancy import tenant_context
from apps.obligations.models import Obligation
from tests.briefing.fixtures import NOW, briefing_setup
from tests.briefing.provider_fixtures import provider_graph

pytestmark = pytest.mark.django_db(transaction=True)


def test_each_users_feedback_changes_only_their_items_and_counts() -> None:
    setup = briefing_setup()
    graph = provider_graph(setup)
    with tenant_context(setup.tenant.pk):
        ClientAssignment.objects.create(
            tenant=setup.tenant, client=setup.client, user=setup.staff
        )
        initial = briefing(setup.actor, scope="team", filters=BriefingFilters())
        colleague = briefing(setup.staff, scope="team", filters=BriefingFilters())
        assert initial.total == colleague.total == 7
        obligation = next(item for item in initial.items if item.kind == "obligation")
        delivery = next(item for item in colleague.items if item.kind == "delivery")
        dismiss(
            setup.actor,
            Dismissal(
                obligation.kind,
                obligation.subject_ref,
                obligation.fact_version,
                "Revisao A",
                obligation.period,
            ),
        )
        dismiss(
            setup.staff,
            Dismissal(
                delivery.kind,
                delivery.subject_ref,
                delivery.fact_version,
                "Revisao B",
                delivery.period,
            ),
        )

        owner_page = briefing(setup.actor, scope="team", filters=BriefingFilters())
        staff_page = briefing(setup.staff, scope="team", filters=BriefingFilters())

        assert obligation.id not in {item.id for item in owner_page.items}
        assert obligation.id in {item.id for item in staff_page.items}
        assert delivery.id in {item.id for item in owner_page.items}
        assert delivery.id not in {item.id for item in staff_page.items}
        expected = dict.fromkeys(graph.subjects, 1)
        assert {row.kind: row.total for row in owner_page.counts} == expected | {
            "obligation": 0,
        }
        assert {row.kind: row.total for row in staff_page.counts} == expected | {
            "delivery": 0,
        }
        assert owner_page.total == staff_page.total == 6

        Obligation.objects.filter(pk=obligation.subject_ref).update(
            resolved_due_date=NOW.date() + timedelta(days=1)
        )
        resurfaced = briefing(setup.actor, scope="team", filters=BriefingFilters())
        unchanged_colleague = briefing(
            setup.staff, scope="team", filters=BriefingFilters()
        )

    assert resurfaced.total == 7
    assert {row.kind: row.total for row in resurfaced.counts} == expected
    assert unchanged_colleague.total == 6
    assert {row.kind: row.total for row in unchanged_colleague.counts} == expected | {
        "delivery": 0,
    }
