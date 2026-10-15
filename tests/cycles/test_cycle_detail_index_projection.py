"""The batched GET digest must equal every retained authoritative source section."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.authz.models import Role
from apps.core.tenancy import tenant_context
from apps.cycles.display_index import SOURCE_FIELDS, display_source_digest
from apps.cycles.display_reads import load_cycle_detail
from apps.cycles.effects.executors import execute
from apps.cycles.index_projection import collect_index, source_digest
from apps.cycles.models import CycleAmendment, EvidenceRequest
from apps.cycles.services.approvals import approve
from apps.cycles.services.completion import propose_reopen
from apps.tenants.models import Membership
from tests.cycles.evidence_fixtures import scenario_a

pytestmark = pytest.mark.django_db(transaction=True)


def test_display_digest_equals_authority_with_all_retained_sources_nonempty(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    # Given real scenario A, then a real approved reopening with an amendment.
    moment = datetime(2026, 9, 20, 12, 0, 0, 120000, tzinfo=UTC)
    scenario = scenario_a(settings, tmp_path, "t36-index-sources")
    world = scenario.setup
    with tenant_context(world.graph.tenant.pk):
        world.cycle.refresh_from_db()
        reviewer = User.objects.create_user(email="reviewer@t36-index-sources.test")
        Membership.objects.create(
            tenant=world.graph.tenant, user=reviewer, role=Role.OPERATIONS_ADMIN
        )
        proposal = propose_reopen(
            world.graph.actor,
            world.cycle.pk,
            reason="Recheck sources without rewriting the completed generation.",
            affected_scope=(world.cycle.pk,),
            expected_version=world.cycle.version,
        )
        approved = approve(reviewer, proposal.pk, expected_version=proposal.version)
        assert execute(
            reviewer, proposal.pk, expected_version=approved.proposal.version
        ).executed
        world.cycle.refresh_from_db()
        assert CycleAmendment.objects.filter(cycle=world.cycle).exists()
        # Pin retained data, not a clock whose model-default callables stay live.
        EvidenceRequest.objects.filter(pk=world.request.pk).update(created_at=moment)
        authoritative = collect_index(world.cycle)
        assert all(authoritative[section] for section in SOURCE_FIELDS)
        expected = source_digest(world.cycle)
        # When the GET's batched source read is materialized.
        reads = load_cycle_detail(world.graph.actor, world.cycle.pk)
        actual = display_source_digest(reads)
    # Then every source hash, timestamp and scoped observation history agrees.
    assert actual == expected
