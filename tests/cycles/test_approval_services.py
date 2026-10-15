"""Exact decisions use real live sources, stored bytes and distinct firm actors."""

from dataclasses import dataclass
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.clients.maintenance import ContactData, save_contact
from apps.clients.models import ClientAssignment, ClientContact
from apps.core.tenancy import tenant_context
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.errors import VersionConflict
from apps.cycles.models import ActionProposal, Approval, CycleEvent, ServiceCycle
from apps.cycles.services.approvals import approve, cancel, propose, reject, revalidate
from apps.cycles.services.cycle import open_cycle
from tests.cycles.cycle_fixtures import PERIOD, CycleSetup, cycle_setup

pytestmark = pytest.mark.django_db(transaction=True)


@dataclass(frozen=True, slots=True)
class ApprovalSetup:
    """An assigned author, an independent owner and a real eligible recipient."""

    graph: CycleSetup
    cycle: ServiceCycle
    contact: ClientContact
    proposal: ActionProposal


@pytest.fixture
def approval_setup() -> ApprovalSetup:
    graph = cycle_setup("approval-service")
    with tenant_context(graph.tenant.pk):
        ClientAssignment.objects.create(
            tenant=graph.tenant, client=graph.client, user=graph.staff
        )
        contact = ClientContact.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            name="Synthetic recipient",
            email="recipient@example.test",
        )
        ref = open_cycle(
            graph.actor,
            graph.engagement.pk,
            PERIOD,
            graph.definition.pk,
            idempotency_key="approval-service",
        )
        cycle = ServiceCycle.objects.get(pk=ref.id)
        proposal = propose(
            graph.staff,
            cycle.pk,
            ProposalDraft(
                ProposalInput(
                    action_kind=ActionKind.EMAIL_NOTICE,
                    step_key="notice",
                    recipient_ids=(contact.pk,),
                    subject="Documentos de setembro",
                    body=(
                        "Os documentos selecionados estão prontos para sua conferência."
                    ),
                ),
                expires_at=timezone.now() + timedelta(days=1),
            ),
            expected_version=cycle.version,
        )
    return ApprovalSetup(graph, cycle, contact, proposal)


def test_independent_approval_binds_exact_persisted_bytes(
    approval_setup: ApprovalSetup,
) -> None:
    setup = approval_setup
    with tenant_context(setup.graph.tenant.pk):
        frozen = bytes(setup.proposal.manifest_bytes)
        result = approve(
            setup.graph.actor,
            setup.proposal.pk,
            expected_version=setup.proposal.version,
        )
        assert result.proposal.state == "aprovada"
        decision = Approval.objects.get(proposal=setup.proposal)
        assert decision.digest == setup.proposal.digest
        assert decision.proposal_revision == setup.proposal.revision
        assert bytes(result.proposal.manifest_bytes) == frozen
        assert revalidate(setup.graph.actor, setup.proposal.pk).executable


def test_changed_selected_contact_invalidates_and_records_no_approval(
    approval_setup: ApprovalSetup,
) -> None:
    setup = approval_setup
    with tenant_context(setup.graph.tenant.pk):
        save_contact(
            actor=setup.graph.actor,
            tenant_id=setup.graph.tenant.pk,
            client_id=setup.graph.client.pk,
            data=ContactData(name=setup.contact.name, email="changed@example.test"),
            contact_id=setup.contact.pk,
            expected_version=setup.contact.version,
        )
        result = approve(
            setup.graph.actor,
            setup.proposal.pk,
            expected_version=setup.proposal.version,
        )
        assert result.proposal.state == "invalidada"
        assert "recipients_changed" in {reason.code for reason in result.reasons}
        assert not Approval.objects.exists()
        assert CycleEvent.objects.filter(type="proposal.invalidated").count() == 1


def test_stale_decision_is_a_no_write_conflict(approval_setup: ApprovalSetup) -> None:
    setup = approval_setup
    with tenant_context(setup.graph.tenant.pk):
        before = CycleEvent.objects.count()
        with pytest.raises(VersionConflict):
            approve(
                setup.graph.actor,
                setup.proposal.pk,
                expected_version=setup.proposal.version + 1,
            )
        setup.proposal.refresh_from_db()
        assert setup.proposal.version == 1
        assert not Approval.objects.exists()
        assert CycleEvent.objects.count() == before


def test_initiator_cannot_self_approve_without_audited_sole_staff_override(
    approval_setup: ApprovalSetup,
) -> None:
    setup = approval_setup
    with tenant_context(setup.graph.tenant.pk):
        with pytest.raises(PermissionDenied):
            approve(
                setup.graph.staff,
                setup.proposal.pk,
                expected_version=setup.proposal.version,
                self_approval_acknowledged=True,
            )
        assert not Approval.objects.exists()


@pytest.mark.parametrize("operation", ["reject", "cancel"])
def test_terminal_decision_cannot_execute(
    approval_setup: ApprovalSetup,
    operation: str,
) -> None:
    setup = approval_setup
    with tenant_context(setup.graph.tenant.pk):
        if operation == "reject":
            result = reject(
                setup.graph.actor,
                setup.proposal.pk,
                expected_version=setup.proposal.version,
                reason="Revisar o contexto.",
            )
            assert result.proposal.state == "rejeitada"
        else:
            result = cancel(
                setup.graph.staff,
                setup.proposal.pk,
                expected_version=setup.proposal.version,
            )
            assert result.proposal.state == "cancelada"
        assert not revalidate(setup.graph.actor, setup.proposal.pk).executable
