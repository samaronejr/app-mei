"""Portal inserts advance request facts even when the portal cannot UPDATE requests."""

import pytest

from apps.briefing.queries import briefing
from apps.briefing.types import BriefingFilters
from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleEvent
from tests.briefing.fixtures import NOW, briefing_setup
from tests.briefing.provider_fixtures import provider_graph

pytestmark = pytest.mark.django_db(transaction=True)


def test_submission_event_advances_the_request_fact_without_updating_request() -> None:
    graph = provider_graph(briefing_setup())
    setup = graph.setup
    with tenant_context(setup.tenant.pk):
        before = next(
            item
            for item in briefing(
                setup.actor, scope="team", filters=BriefingFilters()
            ).items
            if item.kind == "request"
        )
        CycleEvent.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            cycle=graph.cycle,
            actor=setup.actor,
            type="evidence.submitted",
            aggregate_type="EvidenceRequest",
            aggregate_id=graph.request.pk,
            aggregate_version=graph.request.version,
            occurred_at=NOW,
            observed_at=NOW,
        )
        after = next(
            item
            for item in briefing(
                setup.actor, scope="team", filters=BriefingFilters()
            ).items
            if item.kind == "request"
        )

    assert after.fact_version != before.fact_version
