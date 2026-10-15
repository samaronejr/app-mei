"""Reopening creates a new approved generation without rewriting the old proof."""

import copy
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, transaction

from apps.accounts.models import User
from apps.authz.models import Role
from apps.core.tenancy import tenant_context
from apps.cycles.completion_records import CompletionGeneration
from apps.cycles.effects.executors import execute
from apps.cycles.errors import VersionConflict
from apps.cycles.models import (
    ActionExecution,
    CycleAmendment,
    CycleState,
    ProposalState,
)
from apps.cycles.services.approvals import approve
from apps.cycles.services.completion import propose_reopen
from apps.tenants.models import Membership
from tests.cycles.completion_fixtures import close_cycle, completion_setup

pytestmark = pytest.mark.django_db


def test_reopen_preserves_original_generation_and_execution() -> None:
    setup = completion_setup("amendment-history")
    with tenant_context(setup.graph.tenant.pk):
        original_proposal = close_cycle(setup)
        original = CompletionGeneration.objects.get(cycle=setup.cycle)
        frozen = copy.deepcopy(original.snapshot)
        digest = original.digest
        setup.cycle.refresh_from_db()
        reviewer = User.objects.create_user(email="reopen-reviewer@example.test")
        Membership.objects.create(
            tenant=setup.graph.tenant, user=reviewer, role=Role.OPERATIONS_ADMIN
        )
        proposal = propose_reopen(
            setup.graph.actor,
            setup.cycle.pk,
            reason="Conferir nova evidência sem alterar a conclusão original.",
            affected_scope=(setup.cycle.pk,),
            expected_version=setup.cycle.version,
        )
        decision = approve(reviewer, proposal.pk, expected_version=proposal.version)
        assert execute(
            reviewer,
            proposal.pk,
            expected_version=decision.proposal.version,
        ).executed
        setup.cycle.refresh_from_db()
        assert (setup.cycle.state, setup.cycle.amendment) == (CycleState.ACTIVE, 1)
        amendment = CycleAmendment.objects.get(cycle=setup.cycle, number=1)
        assert amendment.completed_snapshot == frozen
        original.refresh_from_db()
        assert (original.snapshot, original.digest) == (frozen, digest)
        assert (
            ActionExecution.objects.filter(proposal_id=original_proposal).count() == 1
        )
        assert amendment.pinned_playbook_version_id == setup.cycle.playbook_version_id


def test_reopen_author_cannot_approve_their_own_executed_history() -> None:
    setup = completion_setup("amendment-separation")
    with tenant_context(setup.graph.tenant.pk):
        close_cycle(setup)
        setup.cycle.refresh_from_db()
        proposal = propose_reopen(
            setup.graph.actor,
            setup.cycle.pk,
            reason="Revisar conclusão.",
            affected_scope=(setup.cycle.pk,),
            expected_version=setup.cycle.version,
        )
        with pytest.raises(PermissionDenied):
            approve(setup.graph.actor, proposal.pk, expected_version=proposal.version)
        assert not CycleAmendment.objects.filter(cycle=setup.cycle).exists()


def test_stale_reopen_form_creates_no_second_proposal() -> None:
    setup = completion_setup("amendment-stale")
    with tenant_context(setup.graph.tenant.pk):
        close_cycle(setup)
        setup.cycle.refresh_from_db()
        rendered = setup.cycle.version
        propose_reopen(
            setup.graph.actor,
            setup.cycle.pk,
            reason="Revisar documento.",
            affected_scope=(setup.cycle.pk,),
            expected_version=rendered,
        )
        with pytest.raises(VersionConflict):
            propose_reopen(
                setup.graph.actor,
                setup.cycle.pk,
                reason="Outra tela antiga.",
                affected_scope=(setup.cycle.pk,),
                expected_version=rendered,
            )
        assert not CycleAmendment.objects.filter(cycle=setup.cycle).exists()


def test_completion_generation_refuses_bulk_history_rewrite() -> None:
    setup = completion_setup("amendment-immutable")
    with tenant_context(setup.graph.tenant.pk):
        close_cycle(setup)
        original = CompletionGeneration.objects.get(cycle=setup.cycle)
        with pytest.raises(DatabaseError), transaction.atomic():
            CompletionGeneration.objects.filter(pk=original.pk).update(
                snapshot={"cycle_id": str(uuid4())}
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            CompletionGeneration.objects.filter(pk=original.pk).delete()


def test_reopening_revision_preserves_previous_proposal_and_requires_new_approval() -> (
    None
):
    setup = completion_setup("amendment-revision")
    with tenant_context(setup.graph.tenant.pk):
        close_cycle(setup)
        setup.cycle.refresh_from_db()
        first = propose_reopen(
            setup.graph.actor,
            setup.cycle.pk,
            reason="Conferir documento.",
            affected_scope=(setup.cycle.pk,),
            expected_version=setup.cycle.version,
        )
        original_bytes = bytes(first.manifest_bytes)
        setup.cycle.refresh_from_db()
        second = propose_reopen(
            setup.graph.actor,
            setup.cycle.pk,
            reason="Conferir o documento corrigido.",
            affected_scope=(setup.cycle.pk,),
            expected_version=setup.cycle.version,
        )
        first.refresh_from_db()
        assert first.state == ProposalState.CANCELLED
        assert bytes(first.manifest_bytes) == original_bytes
        assert (second.previous_id, second.revision, second.state) == (
            first.pk,
            2,
            ProposalState.AWAITING,
        )
        assert not ActionExecution.objects.filter(proposal=second).exists()
        assert not CycleAmendment.objects.filter(cycle=setup.cycle).exists()
