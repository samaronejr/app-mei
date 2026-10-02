"""Live authority and effect checks, with no mocked can() or database operations."""

from dataclasses import replace
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.views import _deactivate
from apps.authz.grants import set_review_capability
from apps.authz.models import RoleGrant, UserCapabilityGrant
from apps.clients.maintenance import add_assignment, assignment_state, remove_assignment
from apps.clients.models import ClientAssignment, ClientCompany
from apps.clients.services import unassign_client
from apps.core.tenancy import tenant_context
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.models import Approval, CycleEvent, ProposalState
from apps.cycles.services.approvals import approve, propose, revalidate
from apps.cycles.services.requests import RequestFields, create_request
from apps.tenants.models import Membership, TenantRole
from tests.cycles.approval_fixtures import ApprovalGraph, approval_graph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def setup() -> ApprovalGraph:
    return approval_graph()


@pytest.mark.parametrize("principal", ["initiator", "approver"])
def test_replacement_portfolio_admission_cannot_reuse_old_approval(
    setup: ApprovalGraph, principal: str
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        proposal = setup.proposal
        approver = setup.graph.actor
        if principal == "approver":
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
                    ProposalInput.from_data(proposal.input_spec),
                    proposal.expires_at,
                    previous_id=proposal.pk,
                    previous_version=1,
                ),
                expected_version=setup.cycle.version,
            )
            approver = setup.graph.staff
        assert approve(approver, proposal.pk, expected_version=1).executable
        decision = Approval.objects.get(proposal=proposal)
        frozen = bytes(proposal.manifest_bytes)
        client = setup.graph.client
        original = ClientAssignment.objects.get(client=client, user=setup.graph.staff)
        remove_assignment(
            actor=setup.graph.actor,
            tenant_id=setup.graph.tenant.pk,
            client_id=client.pk,
            assignment_id=original.pk,
            expected_state=assignment_state(client),
        )
        client.refresh_from_db()
        replacement = add_assignment(
            actor=setup.graph.actor,
            tenant_id=setup.graph.tenant.pk,
            client_id=client.pk,
            user_id=setup.graph.staff.pk,
            expected_state=assignment_state(client),
        )
        assert replacement.pk != original.pk
        result = revalidate(setup.graph.actor, proposal.pk)
        assert not result.executable
        assert result.proposal.state == ProposalState.INVALIDATED
        assert bytes(result.proposal.manifest_bytes) == frozen
        assert Approval.objects.get(pk=decision.pk).digest == proposal.digest


@pytest.mark.parametrize("unrelated", ["other_client", "other_user", "firm_wide"])
def test_unrelated_portfolio_admission_changes_keep_approval_valid(
    setup: ApprovalGraph, unrelated: str
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        client = setup.graph.client
        user = setup.graph.actor
        if unrelated == "other_client":
            client = ClientCompany.objects.create(
                tenant=setup.graph.tenant,
                legal_name="Another synthetic client",
                cnpj="11444777000161",
            )
            user = setup.graph.staff
        elif unrelated == "other_user":
            user = User.objects.create_user(email="another-portfolio@example.test")
            Membership.objects.create(
                tenant=setup.graph.tenant, user=user, role=TenantRole.STAFF_ACCOUNTANT
            )
        original = add_assignment(
            actor=setup.graph.actor,
            tenant_id=setup.graph.tenant.pk,
            client_id=client.pk,
            user_id=user.pk,
            expected_state=assignment_state(client),
        )
        client.refresh_from_db()
        assert approve(
            setup.graph.actor, setup.proposal.pk, expected_version=1
        ).executable
        frozen = bytes(setup.proposal.manifest_bytes)
        remove_assignment(
            actor=setup.graph.actor,
            tenant_id=setup.graph.tenant.pk,
            client_id=client.pk,
            assignment_id=original.pk,
            expected_state=assignment_state(client),
        )
        client.refresh_from_db()
        replacement = add_assignment(
            actor=setup.graph.actor,
            tenant_id=setup.graph.tenant.pk,
            client_id=client.pk,
            user_id=user.pk,
            expected_state=assignment_state(client),
        )
        assert replacement.pk != original.pk
        result = revalidate(setup.graph.actor, setup.proposal.pk)
        assert result.executable
        assert not result.reasons
        assert bytes(result.proposal.manifest_bytes) == frozen


def test_unrelated_request_counter_does_not_change_live_email_effects(
    setup: ApprovalGraph,
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        approved = approve(setup.graph.actor, setup.proposal.pk, expected_version=1)
        before = bytes(approved.proposal.manifest_bytes)
        revision = setup.cycle.effect_revision
        create_request(
            setup.graph.actor,
            setup.cycle.pk,
            RequestFields("Uma solicitação não selecionada"),
            expected_version=setup.cycle.version,
        )
        setup.cycle.refresh_from_db()
        assert setup.cycle.version > revision
        assert setup.cycle.effect_revision == revision
        result = revalidate(setup.graph.actor, setup.proposal.pk)
        assert result.executable
        assert not result.reasons
        assert bytes(result.proposal.manifest_bytes) == before


@pytest.mark.parametrize("mutation", ["member", "user", "role", "portfolio"])
def test_revoked_approved_authority_invalidates_exact_history(
    setup: ApprovalGraph,
    mutation: str,
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        # Grant staff the reviewer capability, then make a NEW owner-authored
        # revision so its approver's authority can be revoked independently.
        member = Membership.objects.for_user(setup.graph.actor).get(
            user=setup.graph.staff, tenant=setup.graph.tenant, client__isnull=True
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
        approve(setup.graph.staff, proposal.pk, expected_version=1)
        decision = Approval.objects.get(proposal=proposal)
        if mutation == "member":
            _deactivate(actor=setup.graph.actor, membership=member)
        elif mutation == "user":
            setup.graph.staff.is_active = False
            setup.graph.staff.save(update_fields=["is_active"])
        elif mutation == "role":
            RoleGrant.objects.filter(
                role=member.role, capability__slug="clients.view_assigned"
            ).update(level="none")
        else:
            assignment = setup.graph.client.assignments.get(user=setup.graph.staff)
            unassign_client(actor=setup.graph.actor, assignment=assignment)
        result = revalidate(setup.graph.actor, proposal.pk)
        assert not result.executable
        assert result.proposal.state == ProposalState.INVALIDATED
        assert Approval.objects.get(pk=decision.pk).digest == proposal.digest
        assert (
            CycleEvent.objects.filter(
                aggregate_id=proposal.pk, type="proposal.invalidated"
            ).count()
            == 1
        )
        again = revalidate(setup.graph.actor, proposal.pk)
        assert again.proposal.version == result.proposal.version


def test_expired_grant_is_not_reused_by_a_stale_actor_object(
    setup: ApprovalGraph,
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        member = Membership.objects.for_user(setup.graph.actor).get(
            user=setup.graph.staff, tenant=setup.graph.tenant, client__isnull=True
        )
        grant = set_review_capability(
            actor=setup.graph.actor,
            membership=member,
            enabled=True,
            expected_version=member.review_grant_version,
        )
        assert grant is not None
        UserCapabilityGrant.objects.filter(pk=grant.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        with pytest.raises(PermissionDenied):
            approve(setup.graph.staff, setup.proposal.pk, expected_version=1)
        assert not Approval.objects.exists()


def test_new_revision_keeps_old_bytes_and_old_approval_only_on_the_old_row(
    setup: ApprovalGraph,
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        approve(setup.graph.actor, setup.proposal.pk, expected_version=1)
        setup.proposal.refresh_from_db()
        before = bytes(setup.proposal.manifest_bytes)
        draft = ProposalDraft(
            replace(
                ProposalInput.from_data(setup.proposal.input_spec),
                body="Uma mensagem diferente, ainda não aprovada.",
            ),
            setup.proposal.expires_at,
            previous_id=setup.proposal.pk,
            previous_version=setup.proposal.version,
        )
        revision = propose(
            setup.graph.staff,
            setup.cycle.pk,
            draft,
            expected_version=setup.cycle.version,
        )
        setup.proposal.refresh_from_db()
        assert revision.revision == setup.proposal.revision + 1
        assert revision.business_intent_key == setup.proposal.business_intent_key
        assert bytes(setup.proposal.manifest_bytes) == before
        assert setup.proposal.state == ProposalState.CANCELLED
        assert Approval.objects.filter(proposal=setup.proposal).count() == 1
        assert not Approval.objects.filter(proposal=revision).exists()
        assert not revalidate(setup.graph.actor, revision.pk).executable
