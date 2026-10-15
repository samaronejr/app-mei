"""Real effects for every closed action kind, with no provider-success shortcut."""

import base64
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.effects.executors import execute
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.errors import IllegalTransition
from apps.cycles.models import (
    ActionExecution,
    InternalNote,
    RequestInternal,
    RequestState,
    ServiceCycle,
)
from apps.cycles.services.approvals import approve, propose
from apps.cycles.services.requests import RequestFields, create_request
from apps.delivery.errors import IntentConflict
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.tasks import dispatch_tenant
from tests.cycles.approval_fixtures import approval_graph
from tests.delivery.smtp import FakeSMTP

pytestmark = pytest.mark.django_db(transaction=True)


def test_email_executes_frozen_mime_once_outside_transaction(
    settings: SettingsWrapper, tmp_path: Path
) -> None:

    settings.DELIVERY_PAUSED = False
    settings.MEDIA_ROOT = tmp_path
    with (
        FakeSMTP() as peer,
        override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="127.0.0.1",
            EMAIL_PORT=peer.port,
            EMAIL_HOST_USER="",
            EMAIL_HOST_PASSWORD="",
            EMAIL_USE_TLS=False,
            EMAIL_USE_SSL=False,
            EMAIL_TIMEOUT=3,
        ),
    ):
        setup = approval_graph("email-execution")
        with tenant_context(setup.graph.tenant.pk):
            approved = approve(setup.graph.actor, setup.proposal.pk, expected_version=1)
            payload = json.loads(bytes(approved.proposal.manifest_bytes))["payload"]
            result = execute(setup.graph.actor, setup.proposal.pk, expected_version=2)
            assert result.executed
            assert not result.replayed
            replay = execute(
                setup.graph.actor,
                setup.proposal.pk,
                expected_version=result.proposal.version,
            )
            assert replay.executed
            assert replay.replayed
            assert (
                OutboundMessage.objects.filter(proposal_id=setup.proposal.pk).count()
                == 1
            )

        observed: list[bool] = []

        def before_io() -> None:
            observed.append(connection.in_atomic_block)

        assert (
            dispatch_tenant(str(setup.graph.tenant.pk), before_network_io=before_io)
            == 1
        )
        assert observed == [False]
        assert peer.received == [
            base64.b64decode(payload["mime_base64"], validate=True)
        ]
        with tenant_context(setup.graph.tenant.pk):
            message = OutboundMessage.objects.get(proposal_id=setup.proposal.pk)
            assert message.state == MessageState.ACCEPTED
            assert message.send_started_at is not None
        assert dispatch_tenant(str(setup.graph.tenant.pk)) == 0
        assert len(peer.received) == 1


def test_changed_digest_cannot_reuse_an_executed_business_intent() -> None:

    setup = approval_graph("intent-conflict")
    with tenant_context(setup.graph.tenant.pk):
        approve(setup.graph.actor, setup.proposal.pk, expected_version=1)
        result = execute(setup.graph.actor, setup.proposal.pk, expected_version=2)
        setup.cycle.refresh_from_db()
        changed = propose(
            setup.graph.staff,
            setup.cycle.pk,
            ProposalDraft(
                replace(
                    ProposalInput.from_data(setup.proposal.input_spec),
                    body="Different authorized copy.",
                ),
                setup.proposal.expires_at,
                previous_id=setup.proposal.pk,
                previous_version=result.proposal.version,
            ),
            expected_version=setup.cycle.version,
        )
        approve(setup.graph.actor, changed.pk, expected_version=1)
        with pytest.raises(IntentConflict):
            execute(setup.graph.actor, changed.pk, expected_version=2)
        assert OutboundMessage.objects.count() == 1


def test_approved_waiver_changes_request_and_keeps_private_reason() -> None:

    setup = approval_graph("waiver-execution")
    with tenant_context(setup.graph.tenant.pk):
        request = create_request(
            setup.graph.actor,
            setup.cycle.pk,
            RequestFields("Comprovante ainda solicitado"),
            expected_version=setup.cycle.version,
        )
        setup.cycle.refresh_from_db()
        proposal = propose(
            setup.graph.staff,
            setup.cycle.pk,
            ProposalDraft(
                ProposalInput(
                    action_kind=ActionKind.REQUEST_WAIVER,
                    step_key="waiver",
                    evidence_request_ids=(request.pk,),
                    reason="Motivo privado conferido por outra pessoa.",
                ),
                timezone.now() + timedelta(days=1),
            ),
            expected_version=setup.cycle.version,
        )
        approve(setup.graph.actor, proposal.pk, expected_version=1)
        assert execute(setup.graph.actor, proposal.pk, expected_version=2).executed
        request.refresh_from_db()
        assert request.state == RequestState.WAIVED
        assert request.version == 2
        assert RequestInternal.objects.get(request=request).waiver_reason == (
            "Motivo privado conferido por outra pessoa."
        )
        assert ActionExecution.objects.get(proposal=proposal).digest == proposal.digest
        assert not OutboundMessage.objects.exists()


@pytest.mark.parametrize("reason", ["", "Encerramento com pendências identificadas."])
def test_completion_executes_real_transition_without_bypassing_evidence_guard(
    reason: str,
) -> None:

    setup = approval_graph("completion-execution")
    with tenant_context(setup.graph.tenant.pk):
        ServiceCycle.objects.filter(pk=setup.cycle.pk).update(state="em_execucao")
        setup.cycle.refresh_from_db()
        proposal = propose(
            setup.graph.staff,
            setup.cycle.pk,
            ProposalDraft(
                ProposalInput(
                    action_kind=ActionKind.CYCLE_COMPLETION,
                    step_key="complete",
                    reason=reason,
                    completion_state="encerrado_com_pendencias"
                    if reason
                    else "concluido",
                ),
                timezone.now() + timedelta(days=1),
            ),
            expected_version=setup.cycle.version,
        )
        approve(setup.graph.actor, proposal.pk, expected_version=1)
        if reason:
            assert execute(setup.graph.actor, proposal.pk, expected_version=2).executed
            setup.cycle.refresh_from_db()
            assert setup.cycle.state == "encerrado_com_pendencias"
            assert InternalNote.objects.filter(cycle=setup.cycle, body=reason).exists()
        else:
            with pytest.raises(IllegalTransition):
                execute(setup.graph.actor, proposal.pk, expected_version=2)
            assert not ActionExecution.objects.filter(proposal=proposal).exists()
            setup.cycle.refresh_from_db()
            assert setup.cycle.state == "em_execucao"
