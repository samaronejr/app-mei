"""The real executor must reject changed effects and revoked approval authority."""

from dataclasses import replace

import pytest

from apps.accounts.views import _deactivate
from apps.authz.grants import set_review_capability
from apps.clients.maintenance import ContactData, save_contact
from apps.core.tenancy import tenant_context
from apps.cycles.effects.executors import execute
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.models import ActionProposal, CycleEvent, ProposalState
from apps.cycles.models import Exception as CycleException
from apps.cycles.services.approvals import approve, propose
from apps.delivery.models import OutboundMessage
from apps.tenants.models import Membership
from tests.cycles.approval_fixtures import ApprovalGraph, approval_graph

pytestmark = pytest.mark.django_db(transaction=True)


def reviewable(slug: str = "executor-revocation") -> ApprovalGraph:
    """Owner proposes; an assigned staff reviewer approves with a real grant."""
    setup = approval_graph(slug)
    with tenant_context(setup.graph.tenant.pk):
        member = Membership.objects.get(
            tenant=setup.graph.tenant, user=setup.graph.staff, client__isnull=True
        )
        set_review_capability(
            actor=setup.graph.actor,
            membership=member,
            enabled=True,
            expected_version=member.review_grant_version,
        )
        proposal = propose(
            setup.graph.actor,
            setup.cycle.pk,
            ProposalDraft(
                ProposalInput.from_data(setup.proposal.input_spec),
                setup.proposal.expires_at,
                previous_id=setup.proposal.pk,
                previous_version=1,
            ),
            expected_version=setup.cycle.version,
        )
        result = approve(setup.graph.staff, proposal.pk, expected_version=1)
    return replace(setup, proposal=result.proposal)


@pytest.mark.parametrize(
    "change", ["recipient", "approver_grant", "approver_membership"]
)
def test_executor_revalidates_exact_effect_and_revocation(change: str) -> None:

    setup = reviewable()
    with tenant_context(setup.graph.tenant.pk):
        if change == "recipient":
            save_contact(
                actor=setup.graph.actor,
                tenant_id=setup.graph.tenant.pk,
                client_id=setup.graph.client.pk,
                contact_id=setup.contact.pk,
                data=ContactData(name=setup.contact.name, email="changed@example.test"),
                expected_version=setup.contact.version,
            )
        else:
            member = Membership.objects.get(
                tenant=setup.graph.tenant,
                user=setup.graph.staff,
                client__isnull=True,
            )
            if change == "approver_membership":
                _deactivate(actor=setup.graph.actor, membership=member)
            else:
                set_review_capability(
                    actor=setup.graph.actor,
                    membership=member,
                    enabled=False,
                    expected_version=member.review_grant_version,
                )
        result = execute(setup.graph.actor, setup.proposal.pk, expected_version=2)
        assert not result.executed
        proposal = ActionProposal.objects.get(pk=setup.proposal.pk)
        assert proposal.state == ProposalState.INVALIDATED
        assert not OutboundMessage.objects.exists()
        assert CycleException.objects.filter(
            subject_ref=proposal.pk, reason_code="approval_invalidated"
        ).exists()
        assert (
            CycleEvent.objects.filter(
                aggregate_id=proposal.pk, type="proposal.invalidated"
            ).count()
            == 1
        )
