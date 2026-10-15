"""Event-ordered real runtime transactions prove the durable dispatch cutpoint."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event

import pytest
from django.db import connection
from pytest_django.fixtures import SettingsWrapper

from apps.authz.grants import set_review_capability
from apps.core.tenancy import tenant_context
from apps.cycles.effects.executors import execute
from apps.cycles.errors import IllegalTransition
from apps.cycles.models import CycleEvent, ProposalState
from apps.cycles.services.approvals import cancel
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.services import claim_batch, prepare_claim
from apps.tenants.models import Membership
from tests.cycles.approval_fixtures import SIGNAL_TIMEOUT, ApprovalGraph, LockSignal
from tests.cycles.test_approval_revocation import reviewable
from tests.support_threads import run_in_thread_as_runtime

pytestmark = pytest.mark.django_db(transaction=True)


def _runtime() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("app_runtime",)


def _revoke(setup: ApprovalGraph) -> None:
    _runtime()
    member = Membership.objects.get(
        tenant=setup.graph.tenant, user=setup.graph.staff, client__isnull=True
    )
    set_review_capability(
        actor=setup.graph.actor,
        membership=member,
        enabled=False,
        expected_version=member.review_grant_version,
    )


@dataclass
class DispatchStartSignal:
    """Pause at the actual durable write after every authorization lock is held."""

    held: Event
    release: Event

    def __call__(
        self,
        execute: Callable[..., object],
        sql: str,
        params: object,
        many: bool,
        context: dict[str, object],
    ) -> object:
        if (
            sql.startswith('UPDATE "delivery_outboundmessage"')
            and 'SET "send_started_at"' in sql
        ):
            self.held.set()
            assert self.release.wait(SIGNAL_TIMEOUT)
        return execute(sql, params, many, context)


@pytest.mark.parametrize("first", ["revocation", "dispatch"])
def test_revocation_and_dispatch_have_one_durable_order(
    settings: SettingsWrapper, first: str
) -> None:

    settings.DELIVERY_PAUSED = False
    setup = reviewable("dispatch-race")
    tid = setup.graph.tenant.pk
    with tenant_context(tid):
        assert execute(
            setup.graph.actor, setup.proposal.pk, expected_version=2
        ).executed
    claims = claim_batch(tid)
    assert len(claims) == 1
    claim = claims[0]
    held, release, entering = Event(), Event(), Event()

    def revoker() -> None:
        if first == "revocation":
            _revoke(setup)
            held.set()
            assert release.wait(SIGNAL_TIMEOUT)
        else:
            with connection.execute_wrapper(LockSignal("accounts_user", entering)):
                _revoke(setup)

    def dispatcher() -> str:
        _runtime()
        signal = (
            DispatchStartSignal(held, release)
            if first == "dispatch"
            else LockSignal("accounts_user", entering)
        )
        with connection.execute_wrapper(signal):
            return str(prepare_claim(claim)[0].state)

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = revoker if first == "revocation" else dispatcher
        follower = dispatcher if first == "revocation" else revoker
        leading = pool.submit(run_in_thread_as_runtime, tid, leader)
        try:
            assert held.wait(SIGNAL_TIMEOUT)
            following = pool.submit(run_in_thread_as_runtime, tid, follower)
            assert entering.wait(SIGNAL_TIMEOUT)
        finally:
            release.set()
        leading_result, following_result = (
            leading.result(timeout=SIGNAL_TIMEOUT),
            following.result(timeout=SIGNAL_TIMEOUT),
        )

    with tenant_context(tid):
        message = OutboundMessage.objects.get(pk=claim.message_id)
        setup.proposal.refresh_from_db()
        if first == "revocation":
            assert following_result == MessageState.CANCELLED
            assert message.send_started_at is None
            assert setup.proposal.state == ProposalState.INVALIDATED
            assert not CycleEvent.objects.filter(
                aggregate_id=setup.proposal.pk,
                type="proposal.dispatch_started",
            ).exists()
        else:
            assert leading_result == MessageState.SENDING
            assert message.send_started_at is not None
            assert (
                CycleEvent.objects.filter(
                    aggregate_id=setup.proposal.pk,
                    type="proposal.dispatch_started",
                ).count()
                == 1
            )
            with pytest.raises(IllegalTransition):
                cancel(
                    setup.graph.actor,
                    setup.proposal.pk,
                    expected_version=setup.proposal.version,
                )
            message.refresh_from_db()
            assert message.state == MessageState.SENDING
            assert message.send_started_at is not None
