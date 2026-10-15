"""The sole-staff exception is explicit, audited and based on actual live users."""

from dataclasses import replace
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.views import _deactivate
from apps.audit.models import Event
from apps.core.tenancy import tenant_context
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.errors import IllegalTransition
from apps.cycles.models import ActionProposal, Approval, ProposalState
from apps.cycles.services.approvals import (
    approve,
    configure_approval_policy,
    propose,
    revalidate,
)
from apps.tenants.models import Membership, TenantRole
from tests.cycles.approval_fixtures import ApprovalGraph, approval_graph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def setup() -> ApprovalGraph:
    return approval_graph("approval-policy")


def _sole(setup: ApprovalGraph, *, inactive_user: bool = False) -> ActionProposal:
    if inactive_user:
        setup.graph.staff.is_active = False
        setup.graph.staff.save(update_fields=["is_active"])
    else:
        member = Membership.objects.for_user(setup.graph.actor).get(
            user=setup.graph.staff, tenant=setup.graph.tenant, client__isnull=True
        )
        _deactivate(actor=setup.graph.actor, membership=member)
    configure_approval_policy(
        setup.graph.actor,
        expected_version=1,
        allow_self_approval=True,
        acknowledged=True,
    )
    return propose(
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


@pytest.mark.parametrize("inactive_user", [False, True])
def test_sole_active_person_must_acknowledge_policy_and_each_self_decision(
    setup: ApprovalGraph,
    inactive_user: bool,
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        proposal = _sole(setup, inactive_user=inactive_user)
        assert Event.objects.filter(object_type="approval_policy").count() == 1
        with pytest.raises(PermissionDenied):
            approve(setup.graph.actor, proposal.pk, expected_version=1)
        assert not Approval.objects.exists()
        result = approve(
            setup.graph.actor,
            proposal.pk,
            expected_version=1,
            self_approval_acknowledged=True,
        )
        assert result.executable
        decision = Approval.objects.get(proposal=proposal)
        assert decision.authorization_snapshot["self_approval_acknowledged"] is True


@pytest.mark.parametrize("acknowledged", [False, True])
def test_two_actual_active_people_cannot_enable_self_approval(
    setup: ApprovalGraph,
    acknowledged: bool,
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        with pytest.raises(ValidationError):
            configure_approval_policy(
                setup.graph.actor,
                expected_version=1,
                allow_self_approval=True,
                acknowledged=acknowledged,
            )
        setup.graph.tenant.refresh_from_db()
        assert not setup.graph.tenant.allow_self_approval
        assert setup.graph.tenant.approval_policy_version == 1
        assert not Event.objects.filter(object_type="approval_policy").exists()


def test_user_reactivation_invalidates_a_previously_valid_self_approval(
    setup: ApprovalGraph,
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        proposal = _sole(setup, inactive_user=True)
        approve(
            setup.graph.actor,
            proposal.pk,
            expected_version=1,
            self_approval_acknowledged=True,
        )
        setup.graph.staff.is_active = True
        setup.graph.staff.save(update_fields=["is_active"])
        result = revalidate(setup.graph.actor, proposal.pk)
        assert result.proposal.state == ProposalState.INVALIDATED
        assert "policy_changed" in {reason.code for reason in result.reasons}
        assert not result.executable
        assert Approval.objects.filter(proposal=proposal).count() == 1


def test_real_membership_insertion_disables_the_sole_staff_override(
    setup: ApprovalGraph,
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        proposal = _sole(setup)
        user = User.objects.create_user(email="new-person@approval-policy.test")
        Membership.objects.create(
            tenant=setup.graph.tenant, user=user, role=TenantRole.STAFF_ACCOUNTANT
        )
        result = approve(
            setup.graph.actor,
            proposal.pk,
            expected_version=1,
            self_approval_acknowledged=True,
        )
        assert result.proposal.state == ProposalState.INVALIDATED
        assert not Approval.objects.exists()


def test_join_then_leave_cannot_revive_a_self_approval_from_an_older_roster(
    setup: ApprovalGraph,
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        proposal = _sole(setup)
        approve(
            setup.graph.actor,
            proposal.pk,
            expected_version=1,
            self_approval_acknowledged=True,
        )
        user = User.objects.create_user(email="temporary@approval-policy.test")
        member = Membership.objects.create(
            tenant=setup.graph.tenant, user=user, role=TenantRole.STAFF_ACCOUNTANT
        )
        _deactivate(actor=setup.graph.actor, membership=member)
        result = revalidate(setup.graph.actor, proposal.pk)
        assert result.proposal.state == ProposalState.INVALIDATED
        assert not result.executable
        assert Approval.objects.filter(proposal=proposal).count() == 1


def test_unrelated_roster_changes_do_not_invalidate_independent_approval(
    setup: ApprovalGraph,
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        approve(setup.graph.actor, setup.proposal.pk, expected_version=1)
        before = bytes(setup.proposal.manifest_bytes)
        user = User.objects.create_user(email="unrelated@approval-policy.test")
        member = Membership.objects.create(
            tenant=setup.graph.tenant, user=user, role=TenantRole.STAFF_ACCOUNTANT
        )
        _deactivate(actor=setup.graph.actor, membership=member)
        result = revalidate(setup.graph.actor, setup.proposal.pk)
        assert result.executable
        assert not result.reasons
        assert bytes(result.proposal.manifest_bytes) == before


def test_quorum_requires_distinct_live_approvers(setup: ApprovalGraph) -> None:
    with tenant_context(setup.graph.tenant.pk):
        configure_approval_policy(
            setup.graph.actor,
            expected_version=1,
            allow_self_approval=False,
            acknowledged=False,
            quorum=2,
        )
        other = User.objects.create_user(email="operations@approval-policy.test")
        Membership.objects.create(
            tenant=setup.graph.tenant, user=other, role=TenantRole.OPERATIONS_ADMIN
        )
        proposal = propose(
            setup.graph.staff,
            setup.cycle.pk,
            ProposalDraft(
                ProposalInput.from_data(setup.proposal.input_spec),
                setup.proposal.expires_at,
                previous_id=setup.proposal.pk,
                previous_version=1,
            ),
            expected_version=setup.cycle.version,
        )
        first = approve(setup.graph.actor, proposal.pk, expected_version=1)
        assert first.proposal.state == ProposalState.AWAITING
        assert not first.executable
        with pytest.raises(IllegalTransition):
            approve(
                setup.graph.actor,
                proposal.pk,
                expected_version=first.proposal.version,
            )
        second = approve(other, proposal.pk, expected_version=first.proposal.version)
        assert second.executable
        assert Approval.objects.filter(proposal=proposal).count() == 2


def test_time_bounds_are_checked_again_without_rebuilding_identity(
    setup: ApprovalGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        start = timezone.now() + timedelta(hours=1)
        end = start + timedelta(hours=1)
        proposal = propose(
            setup.graph.staff,
            setup.cycle.pk,
            ProposalDraft(
                replace(
                    ProposalInput.from_data(setup.proposal.input_spec), occurrence=2
                ),
                end,
                not_before=start,
            ),
            expected_version=setup.cycle.version,
        )
        result = approve(setup.graph.actor, proposal.pk, expected_version=1)
        frozen = bytes(result.proposal.manifest_bytes)
        assert result.proposal.state == ProposalState.APPROVED
        assert not result.executable
        assert "not_before" in {reason.code for reason in result.reasons}
        monkeypatch.setattr(timezone, "now", lambda: start)
        assert revalidate(setup.graph.actor, proposal.pk).executable
        monkeypatch.setattr(timezone, "now", lambda: end)
        expired = revalidate(setup.graph.actor, proposal.pk)
        assert expired.proposal.state == ProposalState.INVALIDATED
        assert bytes(expired.proposal.manifest_bytes) == frozen
