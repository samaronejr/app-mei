"""The automatic grant never bypasses live source, account or dispatch refusals."""

from collections.abc import Callable
from datetime import timedelta
from http import HTTPStatus

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleState
from apps.cycles.services.cycle import transition_cycle
from apps.cycles.services.requests import (
    RequestFields,
    remind_request,
    update_request,
    waive_request,
)
from apps.delivery import gateway, journal, tasks
from apps.delivery.models import MessageState, OutboundMessage, TenantNoticePolicy
from apps.delivery.notice_authority import configure
from apps.delivery.services import claim_batch, set_pause
from tests.delivery import reminder_fixtures
from tests.delivery.reminder_fixtures import NOW, allow_automatic, reminders
from tests.delivery.smtp import FakeSMTP
from tests.portal.request_fixtures import PortalSetup, upload

pytestmark = pytest.mark.django_db(transaction=True)
reminder_setup = reminder_fixtures.reminder_setup


@pytest.mark.parametrize(
    "negative",
    [
        "published_only",
        "manual_only",
        "unacknowledged",
        "tenant_pause",
        "global_pause",
        "disabled",
        "global_disabled",
        "read_only",
    ],
)
def test_ineligible_tenant_never_enqueues_or_sends(
    reminder_setup: PortalSetup,
    smtp_server: Callable[[str], FakeSMTP],
    settings: SettingsWrapper,
    negative: str,
) -> None:
    # Given a real published cycle; neither publication nor a manual grant opts it in.
    setup = reminder_setup
    if negative == "manual_only":
        with tenant_context(setup.graph.tenant.pk):
            configure(
                setup.graph.actor,
                kinds=("request_reminder",),
                acknowledged=True,
                expected_version=0,
            )
    elif negative == "unacknowledged":
        with tenant_context(setup.graph.tenant.pk):
            TenantNoticePolicy.objects.create(
                tenant=setup.graph.tenant,
                grants_json=[
                    {
                        "kind": "request_reminder",
                        "mode": "automatic",
                        "rule_version": 1,
                    }
                ],
            )
    elif negative != "published_only":
        allow_automatic(setup)
        if negative == "tenant_pause":
            with tenant_context(setup.graph.tenant.pk):
                set_pause(setup.graph.actor, paused=True, expected_version=1)
        elif negative == "global_pause":
            settings.DELIVERY_PAUSED = True
        elif negative == "disabled":
            setup.graph.tenant.features = []
            setup.graph.tenant.save(update_fields=["features"])
        elif negative == "global_disabled":
            settings.AXORIM_DISABLED_FEATURES = ("cycles_r1",)
        else:
            setup.graph.tenant.cancellation_ends_on = timezone.localdate(NOW) - (
                timedelta(days=1)
            )
            setup.graph.tenant.save(update_fields=["cancellation_ends_on"])
    server = smtp_server("accept")

    # When the sweep and the real outbox dispatcher visit this tenant.
    result = tasks.sweep_reminders(clock=lambda: NOW)
    tasks.dispatch_outbox()

    # Then negative authority, pause and account states create no SMTP opportunity.
    assert result["queued"] == 0
    assert reminders(setup) == []
    assert server.received == []
    assert journal.read(setup.graph.tenant.pk) == []


def test_upload_before_dispatch_cancels_without_journal_or_smtp(
    reminder_setup: PortalSetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    # Given an automatic occurrence already claimed, but not started.
    setup = reminder_setup
    allow_automatic(setup)
    tasks.sweep_reminders(clock=lambda: NOW)
    [message] = reminders(setup)
    [claim] = claim_batch(setup.graph.tenant.pk)
    server = smtp_server("accept")

    # When a real portal upload commits before the existing gateway starts dispatch.
    uploaded = setup.portal().post(
        f"/solicitacoes/{setup.request.pk}/enviar",
        {"file": upload("extrato.pdf", b"%PDF-1.4 T39 synthetic upload\n")},
    )
    assert uploaded.status_code == HTTPStatus.FOUND
    reply = gateway.send(claim)

    # Then source re-evaluation cancels before token preparation, journal or SMTP.
    assert (reply.state, reply.code) == (MessageState.CANCELLED, "request_submitted")
    with tenant_context(setup.graph.tenant.pk):
        message.refresh_from_db()
        assert message.send_started_at is None
        assert message.state == MessageState.CANCELLED
    assert journal.read(setup.graph.tenant.pk) == []
    assert server.received == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("waiver", "request_closed"),
        ("closure", "cycle_closed"),
        ("not_overdue", "request_not_overdue"),
        ("expiry", "reminder_expired"),
        ("pause", "delivery_paused"),
        ("flag", "delivery_paused"),
        ("revocation", "notice_policy_unacknowledged"),
        ("read_only", "issuer_not_allowed"),
    ],
)
def test_obsolete_claim_cancels_before_any_external_effect(
    reminder_setup: PortalSetup,
    smtp_server: Callable[[str], FakeSMTP],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    reason: str,
) -> None:
    # Given an immutable automatic occurrence and an exact pre-start control change.
    setup = reminder_setup
    allow_automatic(setup)
    tasks.sweep_reminders(clock=lambda: NOW)
    if change == "expiry":
        monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(days=1))
    [claim] = claim_batch(setup.graph.tenant.pk)
    with tenant_context(setup.graph.tenant.pk):
        if change == "waiver":
            waive_request(
                setup.graph.actor,
                setup.request.pk,
                reason="Synthetic waiver",
                expected_version=setup.request.version,
            )
        elif change == "closure":
            setup.cycle.refresh_from_db()
            transition_cycle(
                setup.graph.actor,
                setup.cycle.pk,
                CycleState.CANCELLED,
                expected_version=setup.cycle.version,
            )
        elif change == "not_overdue":
            update_request(
                setup.graph.actor,
                setup.request.pk,
                RequestFields(
                    title=setup.request.title, due_on=timezone.localdate(NOW)
                ),
                expected_version=setup.request.version,
            )
        elif change == "pause":
            set_pause(setup.graph.actor, paused=True, expected_version=1)
        elif change == "flag":
            setup.graph.tenant.features = []
            setup.graph.tenant.save(update_fields=["features"])
        elif change == "revocation":
            configure(
                setup.graph.actor,
                kinds=(),
                acknowledged=False,
                expected_version=1,
            )
        elif change == "read_only":
            setup.graph.tenant.cancellation_ends_on = timezone.localdate(NOW) - (
                timedelta(days=1)
            )
            setup.graph.tenant.save(update_fields=["cancellation_ends_on"])
    server = smtp_server("accept")

    # When the already-claimed occurrence reaches the real gateway.
    reply = gateway.send(claim)

    # Then even a valid old claim is not consent to start the obsolete reminder.
    assert (reply.state, reply.code) == (MessageState.CANCELLED, reason)
    assert journal.read(setup.graph.tenant.pk) == []
    assert server.received == []


def test_automatic_binding_is_immutable_and_reacknowledgement_keeps_the_key(
    reminder_setup: PortalSetup,
) -> None:
    # Given an automatic occurrence bound to revision 1.
    setup = reminder_setup
    allow_automatic(setup)
    tasks.sweep_reminders(clock=lambda: NOW)
    [message] = reminders(setup)
    with tenant_context(setup.graph.tenant.pk):
        with pytest.raises(IntegrityError), transaction.atomic():
            OutboundMessage.objects.filter(pk=message.pk).update(
                notice_authorization_bytes=None,
                notice_authorization_digest=None,
            )
        configure(
            setup.graph.actor,
            kinds=(),
            automatic_reminders=True,
            acknowledged=True,
            expected_version=1,
        )

    # When the same natural occurrence is reconsidered under another acknowledgement.
    result = tasks.sweep_reminders(clock=lambda: NOW)

    # Then authority conflicts, rather than a revision-keyed second effect, are visible.
    assert result["queued"] == 0
    assert result["conflicts"] == 1
    [stored] = reminders(setup)
    assert stored.pk == message.pk
    assert stored.business_intent_key == message.business_intent_key
    assert stored.notice_authorization_bytes == message.notice_authorization_bytes


def test_manual_reminder_still_uses_publication_without_automatic_policy(
    reminder_setup: PortalSetup,
) -> None:
    # Given no notice policy and the real published-cycle manual command.
    setup = reminder_setup

    # When the owner manually requests a reminder.
    with tenant_context(setup.graph.tenant.pk):
        remind_request(
            setup.graph.actor,
            setup.request.pk,
            expected_version=setup.request.version,
        )

    # Then it remains independently authorized, and the sweep adds nothing.
    assert len(reminders(setup)) == 1
    assert tasks.sweep_reminders(clock=lambda: NOW)["queued"] == 0
