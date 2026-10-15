"""The briefing consumes T17's public answer without its own freshness policy."""

import pytest

from apps.briefing.coverage_source import coverage_items
from apps.core.tenancy import tenant_context
from apps.obligations.coverage import CoverageState, coverage_for
from apps.obligations.models import ObservationVerificationStatus
from apps.obligations.observations import assert_fact
from tests.briefing.fixtures import NOW, briefing_setup, due_obligation
from tests.cycles.cycle_fixtures import PERIOD

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("source_kind", "verification", "expected"),
    [
        (
            "staff_recorded",
            ObservationVerificationStatus.UNKNOWN,
            CoverageState.MANUAL,
        ),
        (
            "official_api",
            ObservationVerificationStatus.STALE,
            CoverageState.STALE,
        ),
        (
            "official_api",
            ObservationVerificationStatus.REFRESH_FAILED,
            CoverageState.REFRESH_FAILED,
        ),
    ],
)
def test_provider_preserves_the_authoritative_coverage_answer(
    source_kind: str, verification: str, expected: str
) -> None:
    setup = briefing_setup()
    subject = due_obligation(setup)
    with tenant_context(setup.tenant.pk):
        assert_fact(
            tenant_id=setup.tenant.pk,
            client_id=setup.client.pk,
            subject_id=subject.pk,
            kind="payment",
            period=PERIOD,
            source_kind=source_kind,
            observed_at=NOW,
            value={},
            verification_status=verification,
            outcome="recorded",
        )
        ordinary = coverage_for(setup.actor, setup.client, PERIOD, now=NOW)
        assert ordinary[0].state == expected
        items = coverage_items(setup.actor, [setup.client], PERIOD, now=NOW)

    if expected == CoverageState.MANUAL:
        assert items == []
    else:
        assert len(items) == 1
        assert items[0].freshness == expected
        assert items[0].subject_ref == ordinary[0].engagement_id
        assert items[0].source_observed_at == ordinary[0].last_success
        assert items[0].priority == 10
