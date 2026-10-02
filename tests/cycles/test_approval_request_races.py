"""Existing send/remind writers compose with T15's client-before-cycle order."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from django.db import OperationalError, connection, transaction
from django.utils import timezone
from psycopg.errors import LockNotAvailable

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.errors import VersionConflict
from apps.cycles.models import ActionProposal, RequestState
from apps.cycles.services.approvals import propose
from apps.cycles.services.requests import (
    RequestFields,
    create_request,
    remind_request,
    send_requests,
)
from tests.cycles.approval_fixtures import SIGNAL_TIMEOUT, LockSignal
from tests.portal.request_fixtures import PortalSetup, portal_setup
from tests.support_threads import run_in_thread_as_runtime

pytestmark = pytest.mark.django_db(transaction=True)


def _proposal(setup: PortalSetup, version: int) -> None:
    propose(
        setup.graph.actor,
        setup.cycle.pk,
        ProposalDraft(
            ProposalInput(
                action_kind=ActionKind.EMAIL_NOTICE,
                step_key="composition",
                recipient_ids=(setup.contact.pk,),
                subject="Bound message",
                body="Exact text.",
            ),
            timezone.now() + timedelta(days=1),
        ),
        expected_version=version,
    )


def _request(setup: PortalSetup, kind: str, version: int) -> None:
    if kind == "send":
        send_requests(setup.graph.actor, setup.cycle.pk, expected_version=version)
    else:
        remind_request(setup.graph.actor, setup.request.pk, expected_version=version)


def _probe_client(setup: PortalSetup) -> None:
    assert ClientCompany.objects.filter(pk=setup.graph.client.pk).exists()
    with transaction.atomic():
        ClientCompany.objects.select_for_update(no_key=True, nowait=True).get(
            pk=setup.graph.client.pk
        )


@pytest.mark.parametrize("kind", ["send", "remind"])
@pytest.mark.parametrize("first", ["proposal", "request"])
def test_existing_request_writers_and_proposals_serialize_before_cycle(
    kind: str,
    first: str,
) -> None:
    setup = portal_setup("approval-request-composition")
    with tenant_context(setup.graph.tenant.pk):
        if kind == "send":
            setup.request = create_request(
                setup.graph.actor,
                setup.cycle.pk,
                RequestFields(
                    title="Another genuine request",
                    client_note="Selected client material.",
                ),
                expected_version=setup.cycle.version,
            )
            setup.cycle.refresh_from_db()
        request_version = (
            setup.cycle.version if kind == "send" else setup.request.version
        )
        cycle_version = setup.cycle.version
    entering = Event()

    def contender() -> str:
        with connection.execute_wrapper(LockSignal("clients_clientcompany", entering)):
            try:
                if first == "proposal":
                    _request(setup, kind, request_version)
                else:
                    _proposal(setup, cycle_version)
            except VersionConflict:
                return "conflict"
            return "saved"

    with ThreadPoolExecutor(max_workers=1) as pool:
        with tenant_context(setup.graph.tenant.pk), transaction.atomic():
            if first == "proposal":
                _proposal(setup, cycle_version)
            else:
                _request(setup, kind, request_version)
            with pytest.raises(OperationalError) as blocked:
                run_in_thread_as_runtime(
                    setup.graph.tenant.pk, lambda: _probe_client(setup)
                )
            assert isinstance(blocked.value.__cause__, LockNotAvailable)
            work = pool.submit(
                run_in_thread_as_runtime, setup.graph.tenant.pk, contender
            )
            assert entering.wait(SIGNAL_TIMEOUT)
        outcome = work.result(timeout=SIGNAL_TIMEOUT)
    assert outcome == ("conflict" if kind == "send" else "saved")
    with tenant_context(setup.graph.tenant.pk):
        setup.cycle.refresh_from_db()
        setup.request.refresh_from_db()
        if outcome == "conflict":
            if first == "proposal":
                assert setup.request.state == RequestState.DRAFT
                _request(setup, kind, setup.cycle.version)
            else:
                assert not ActionProposal.objects.filter(cycle=setup.cycle).exists()
                _proposal(setup, setup.cycle.version)
        setup.request.refresh_from_db()
        assert setup.request.state == RequestState.REQUESTED
        assert ActionProposal.objects.filter(cycle=setup.cycle).count() == 1
