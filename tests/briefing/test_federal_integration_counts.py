"""AXRM-018 preserves positive integration counts in the public briefing."""

import pytest

from apps.briefing.queries import briefing
from apps.briefing.types import BriefingFilters
from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleState, ServiceCycle
from apps.integrations.models import IntegrationIssue
from tests.briefing.fixtures import NOW, briefing_setup

pytestmark = pytest.mark.django_db(transaction=True)


def test_nonzero_integration_count_appears_in_briefing() -> None:
    setup = briefing_setup()
    with tenant_context(setup.tenant.pk):
        ServiceCycle.objects.update(state=CycleState.COMPLETED)
        issue = IntegrationIssue.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            reason="autorizacao_ausente",
            service="pagtoweb",
            owner=setup.actor,
            review_on=NOW.date(),
        )
        page = briefing(setup.actor, scope="team", filters=BriefingFilters())
        counts = {count.kind: count.total for count in page.counts}
        assert counts.get("integration") == 1
        assert [
            item.subject_ref for item in page.items if item.kind == "integration"
        ] == [issue.pk]
