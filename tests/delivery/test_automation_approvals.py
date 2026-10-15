"""Explicit retries cannot escape the exact approval that authorized the mail."""

import base64
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.cycles.effects.executors import execute
from apps.cycles.models import ActionExecution, Approval
from apps.cycles.services.approvals import approve
from apps.delivery.automations import retry_dead_letter
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.tasks import dispatch_tenant
from tests.cycles.approval_fixtures import approval_graph
from tests.delivery.smtp import FakeSMTP

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("recipient_changed", [False, True])
def test_approved_retry_remains_bound_to_frozen_effect_and_live_recipient(
    settings: SettingsWrapper,
    tmp_path: Path,
    smtp_server: Callable[[str], FakeSMTP],
    recipient_changed: bool,
) -> None:
    # Given a genuinely approved frozen envelope definitively refused by SMTP.
    settings.DELIVERY_PAUSED = False
    settings.MEDIA_ROOT = tmp_path
    peer = smtp_server("reject")
    setup = approval_graph("automation-approved")
    with tenant_context(setup.graph.tenant.pk):
        result = approve(setup.graph.actor, setup.proposal.pk, expected_version=1)
        payload = json.loads(bytes(result.proposal.manifest_bytes))["payload"]
        execute(setup.graph.actor, setup.proposal.pk, expected_version=2)
    assert dispatch_tenant(str(setup.graph.tenant.pk)) == 1
    successor: OutboundMessage | None = None
    with tenant_context(setup.graph.tenant.pk):
        message = OutboundMessage.objects.get(proposal_id=setup.proposal.pk)
        assert message.state == MessageState.REJECTED
        approvals = Approval.objects.count()
        if recipient_changed:
            ClientContact.objects.filter(pk=setup.contact.pk).update(
                email="changed@example.test"
            )

            # When the frozen recipient is no longer the live authorized recipient.
            with pytest.raises(ValidationError):
                retry_dead_letter(
                    setup.graph.actor, message.pk, expected_version=message.version
                )

            # Then nothing can create a successor under the stale approval.
            assert OutboundMessage.objects.count() == 1
            assert len(peer.received) == 1
        else:
            # When the manager repeats the unchanged effect.
            successor = retry_dead_letter(
                setup.graph.actor, message.pk, expected_version=message.version
            )
            assert successor.proposal_id == setup.proposal.pk
        assert Approval.objects.count() == approvals
        assert ActionExecution.objects.count() == 1
    peer.mode = "accept"
    sent = dispatch_tenant(str(setup.graph.tenant.pk))
    assert sent == int(not recipient_changed)
    assert len(peer.received) == (1 if recipient_changed else 2)
    if not recipient_changed:
        assert successor is not None
        assert peer.received[1] == base64.b64decode(
            payload["mime_base64"], validate=True
        )
        with tenant_context(setup.graph.tenant.pk):
            successor.refresh_from_db()
            assert successor.state == MessageState.ACCEPTED
    assert dispatch_tenant(str(setup.graph.tenant.pk)) == 0
