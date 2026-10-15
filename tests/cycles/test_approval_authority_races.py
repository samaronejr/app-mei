"""Real authority writers and decisions respect the same participant fences."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest
from django.core.exceptions import PermissionDenied
from django.db import OperationalError, connection, transaction
from django.http import Http404
from psycopg.errors import LockNotAvailable

from apps.accounts.models import User
from apps.accounts.views import _deactivate
from apps.authz.grants import set_review_capability
from apps.clients.models import ClientAssignment, ClientCompany
from apps.clients.services import unassign_client
from apps.core.tenancy import tenant_context
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.models import Approval, ProposalState
from apps.cycles.services.approvals import approve, propose, revalidate
from apps.tenants.models import Membership
from tests.cycles.approval_fixtures import (
    SIGNAL_TIMEOUT,
    ApprovalGraph,
    LockSignal,
    approval_graph,
)
from tests.support_threads import run_in_thread_as_runtime

pytestmark = pytest.mark.django_db(transaction=True)


def _reviewable() -> ApprovalGraph:
    setup = approval_graph("approval-authority-race")
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
    return replace(setup, proposal=proposal)


def _revoke(setup: ApprovalGraph, kind: str) -> None:
    if kind == "portfolio":
        assignment = ClientAssignment.objects.get(
            client=setup.graph.client, user=setup.graph.staff
        )
        unassign_client(actor=setup.graph.actor, assignment=assignment)
    elif kind == "user":
        with transaction.atomic():
            user = User.objects.select_for_update(no_key=True).get(
                pk=setup.graph.staff.pk
            )
            user.is_active = False
            user.save(update_fields=["is_active"])
    else:
        member = Membership.objects.get(
            tenant=setup.graph.tenant, user=setup.graph.staff, client__isnull=True
        )
        if kind == "grant":
            set_review_capability(
                actor=setup.graph.actor,
                membership=member,
                enabled=False,
                expected_version=member.review_grant_version,
            )
        else:
            _deactivate(actor=setup.graph.actor, membership=member)


def _probe_fence(setup: ApprovalGraph, kind: str) -> None:
    with transaction.atomic():
        if kind == "portfolio":
            assert ClientCompany.objects.filter(pk=setup.graph.client.pk).exists()
            ClientCompany.objects.select_for_update(no_key=True, nowait=True).get(
                pk=setup.graph.client.pk
            )
        else:
            assert User.objects.filter(pk=setup.graph.staff.pk).exists()
            User.objects.select_for_update(no_key=True, nowait=True).get(
                pk=setup.graph.staff.pk
            )


@pytest.mark.parametrize("kind", ["grant", "membership", "portfolio", "user"])
def test_revocation_first_is_seen_before_any_approval_is_recorded(kind: str) -> None:
    setup = _reviewable()
    entering = Event()
    table = "clients_clientcompany" if kind == "portfolio" else "accounts_user"

    def decide() -> str:
        with connection.execute_wrapper(LockSignal(table, entering)):
            try:
                result = approve(
                    setup.graph.staff, setup.proposal.pk, expected_version=1
                )
            except (PermissionDenied, Http404):
                return "denied"
            return result.proposal.state

    with ThreadPoolExecutor(max_workers=1) as pool:
        with tenant_context(setup.graph.tenant.pk), transaction.atomic():
            _revoke(setup, kind)
            with pytest.raises(OperationalError) as blocked:
                run_in_thread_as_runtime(
                    setup.graph.tenant.pk, lambda: _probe_fence(setup, kind)
                )
            assert isinstance(blocked.value.__cause__, LockNotAvailable)
            work = pool.submit(run_in_thread_as_runtime, setup.graph.tenant.pk, decide)
            assert entering.wait(SIGNAL_TIMEOUT)
        assert work.result(timeout=SIGNAL_TIMEOUT) == "denied"
    with tenant_context(setup.graph.tenant.pk):
        setup.proposal.refresh_from_db()
        assert setup.proposal.state == ProposalState.AWAITING
        assert not Approval.objects.filter(proposal=setup.proposal).exists()


@pytest.mark.parametrize("kind", ["grant", "membership", "portfolio", "user"])
def test_approval_first_fences_revocation_and_next_revalidation_refuses(
    kind: str,
) -> None:
    setup = _reviewable()
    held, release, entering = Event(), Event(), Event()
    table = "clients_clientcompany" if kind == "portfolio" else "accounts_user"

    def decide() -> str:
        with connection.execute_wrapper(
            LockSignal("cycles_actionproposal", held, release, after=True)
        ):
            return approve(
                setup.graph.staff, setup.proposal.pk, expected_version=1
            ).proposal.state

    def revoke() -> None:
        with connection.execute_wrapper(LockSignal(table, entering)):
            _revoke(setup, kind)

    with ThreadPoolExecutor(max_workers=2) as pool:
        deciding = pool.submit(run_in_thread_as_runtime, setup.graph.tenant.pk, decide)
        try:
            assert held.wait(SIGNAL_TIMEOUT)
            with pytest.raises(OperationalError) as blocked:
                run_in_thread_as_runtime(
                    setup.graph.tenant.pk, lambda: _probe_fence(setup, kind)
                )
            assert isinstance(blocked.value.__cause__, LockNotAvailable)
            revoking = pool.submit(
                run_in_thread_as_runtime, setup.graph.tenant.pk, revoke
            )
            assert entering.wait(SIGNAL_TIMEOUT)
        finally:
            release.set()
        assert deciding.result(timeout=SIGNAL_TIMEOUT) == ProposalState.APPROVED
        revoking.result(timeout=SIGNAL_TIMEOUT)
    with tenant_context(setup.graph.tenant.pk):
        result = revalidate(setup.graph.actor, setup.proposal.pk, expected_version=2)
        assert result.proposal.state == ProposalState.INVALIDATED
        assert not result.executable
        assert Approval.objects.filter(proposal=setup.proposal).count() == 1
