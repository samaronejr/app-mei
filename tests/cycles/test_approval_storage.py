"""Database backstops: real scoped rows, frozen bytes and append-only decisions."""

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import timedelta

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction

from apps.core.identifiers import uuid7
from apps.core.tenancy import tenant_context
from apps.cycles.effects.manifest import ActionKind, JSONValue, canonicalize
from apps.cycles.models import ActionProposal, Approval, ProposalState, ServiceCycle
from apps.cycles.services.cycle import open_cycle
from apps.tenants.models import Membership
from tests.cycles.cycle_fixtures import PERIOD, CycleSetup, cycle_setup
from tests.cycles.manifest_fixtures import HASH, NOW, manifest_example

pytestmark = pytest.mark.django_db(transaction=True)


@dataclass(frozen=True, slots=True)
class Stored:
    graph: CycleSetup
    cycle: ServiceCycle
    proposal: ActionProposal
    membership: Membership


@pytest.fixture
def stored() -> Stored:
    graph = cycle_setup("approval-storage")
    with tenant_context(graph.tenant.pk):
        cycle_ref = open_cycle(
            graph.actor,
            graph.engagement.pk,
            PERIOD,
            graph.definition.pk,
            idempotency_key="approval-storage",
        )
        cycle = ServiceCycle.objects.get(pk=cycle_ref.id)
        membership = Membership.objects.for_user(graph.actor).get(
            tenant=graph.tenant, user=graph.actor, client__isnull=True
        )
        author = Membership.objects.for_user(graph.actor).get(
            tenant=graph.tenant, user=graph.staff, client__isnull=True
        )
        proposal_id = uuid7()
        manifest = replace(
            manifest_example(),
            tenant_id=str(graph.tenant.pk),
            client_id=str(graph.client.pk),
            engagement_id=str(graph.engagement.pk),
            cycle_id=str(cycle.pk),
            cycle_revision=cycle.effect_revision,
            amendment=cycle.amendment,
            period_start=cycle.period_start,
            period_end=cycle.period_end + timedelta(days=1),
            playbook_version_id=str(graph.definition.pk),
            proposal_id=str(proposal_id),
            proposal_revision=1,
            initiator_id=str(graph.staff.pk),
            action_kind=ActionKind.CYCLE_COMPLETION,
            destination={
                "provider": "cycles",
                "account_id": str(graph.tenant.pk),
                "operation": "complete",
            },
            payload={"reason": "Synthetic completed work"},
            recipients=(),
            money=(),
            documents=(),
            evidence=(),
            observations=(),
            tax_profile_versions=(str(cycle.tax_profile_id),),
            policy={
                "id": str(graph.tenant.pk),
                "version": graph.tenant.approval_policy_version,
                "hash": HASH,
            },
            authorization={
                "membership_id": str(author.pk),
                "membership_version": author.version,
                "portfolio_assignment_id": None,
                "grants": (),
            },
            approval_rule={
                "required_capability": "actions.approve",
                "quorum": 1,
                "separation_of_duties": True,
            },
        )
        proposal = ActionProposal.objects.create(
            id=proposal_id,
            tenant=graph.tenant,
            client=graph.client,
            cycle=cycle,
            revision=1,
            business_intent_key=manifest.business_intent_key,
            action_kind=manifest.action_kind,
            input_spec={
                "step_key": "complete",
                "occurrence": 1,
                "subject": "",
                "body": "",
                "reason": "Synthetic completed work",
                "recipients": [],
                "document_links": [],
                "evidence_requests": [],
                "observations": [],
                "money": [],
            },
            manifest_bytes=manifest.canonical_bytes(),
            digest=manifest.digest,
            state=ProposalState.AWAITING,
            initiator=graph.staff,
            expires_at=manifest.expires_at,
        )
    return Stored(graph, cycle, proposal, membership)


def _decision(stored: Stored) -> Approval:
    return Approval.objects.create(
        tenant=stored.graph.tenant,
        client=stored.graph.client,
        proposal=stored.proposal,
        proposal_revision=stored.proposal.revision,
        digest=stored.proposal.digest,
        approver=stored.graph.actor,
        membership_id=stored.membership.pk,
        membership_version=stored.membership.version,
        authorization_snapshot={"grants": []},
        decision="approve",
        decided_at=NOW,
        expires_at=stored.proposal.expires_at,
    )


def test_frozen_manifest_and_digest_cannot_be_rewritten(stored: Stored) -> None:
    with tenant_context(stored.graph.tenant.pk):
        before = bytes(stored.proposal.manifest_bytes)
        with pytest.raises(DatabaseError), transaction.atomic():
            ActionProposal.objects.filter(pk=stored.proposal.pk).update(
                manifest_bytes=b"{}", digest="0" * 64
            )
        stored.proposal.refresh_from_db()
        assert bytes(stored.proposal.manifest_bytes) == before


def test_approval_update_and_delete_are_refused(stored: Stored) -> None:
    with tenant_context(stored.graph.tenant.pk):
        decision = _decision(stored)
        with pytest.raises(DatabaseError), transaction.atomic():
            Approval.objects.filter(pk=decision.pk).update(decision="reject")
        with pytest.raises(DatabaseError), transaction.atomic():
            Approval.objects.filter(pk=decision.pk).delete()
        decision.refresh_from_db()
        assert decision.decision == "approve"


def test_approval_must_bind_the_exact_proposal_revision_and_digest(
    stored: Stored,
) -> None:
    with tenant_context(stored.graph.tenant.pk):
        stored.proposal.digest = "0" * 64
        with pytest.raises(IntegrityError), transaction.atomic():
            _ = _decision(stored)
        assert not Approval.objects.exists()


def test_proposal_client_pair_cannot_reference_another_clients_cycle(
    stored: Stored,
) -> None:
    other = cycle_setup("approval-storage-other")
    with tenant_context(other.tenant.pk):
        ref = open_cycle(
            other.actor,
            other.engagement.pk,
            PERIOD,
            other.definition.pk,
            idempotency_key="other-cycle",
        )
    with tenant_context(stored.graph.tenant.pk):
        proposal_id = uuid7()
        data: dict[str, JSONValue] = json.loads(bytes(stored.proposal.manifest_bytes))
        data.update(
            {
                "cycle_id": str(ref.id),
                "proposal_id": str(proposal_id),
                "proposal_revision": 2,
            }
        )
        raw = canonicalize(data)
        with (
            pytest.raises(IntegrityError, match="actionproposal_cycle_client_fk"),
            transaction.atomic(),
        ):
            ActionProposal.objects.create(
                id=proposal_id,
                tenant=stored.graph.tenant,
                client=stored.graph.client,
                cycle_id=ref.id,
                revision=2,
                business_intent_key=stored.proposal.business_intent_key,
                action_kind=stored.proposal.action_kind,
                input_spec=stored.proposal.input_spec,
                manifest_bytes=raw,
                digest=hashlib.sha256(raw).hexdigest(),
                state=ProposalState.AWAITING,
                initiator=stored.graph.staff,
                expires_at=stored.proposal.expires_at,
            )
        assert ActionProposal.objects.count() == 1


def test_portal_cannot_read_proposals_or_approvals_even_with_an_accidental_grant(
    stored: Stored,
) -> None:
    with tenant_context(stored.graph.tenant.pk), connection.cursor() as cursor:
        _ = _decision(stored)
        assert ActionProposal.objects.filter(pk=stored.proposal.pk).exists()
        assert Approval.objects.filter(proposal=stored.proposal).count() == 1
        cursor.execute("RESET ROLE")
        cursor.execute(
            "GRANT SELECT ON cycles_actionproposal, cycles_approval TO app_portal"
        )
        cursor.execute("SET LOCAL ROLE app_portal")
        cursor.execute(
            "SELECT set_config('app.client_id', %s, true)",
            [str(stored.graph.client.pk)],
        )
        try:
            cursor.execute("SELECT id FROM cycles_actionproposal")
            assert cursor.fetchall() == []
            cursor.execute("SELECT id FROM cycles_approval")
            assert cursor.fetchall() == []
        finally:
            cursor.execute("RESET ROLE")
            cursor.execute(
                "REVOKE SELECT ON cycles_actionproposal, cycles_approval "
                "FROM app_portal"
            )
            cursor.execute("SET ROLE app_runtime")
