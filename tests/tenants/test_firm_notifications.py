"""Private firm notices use the outbox, live authority and conservative replay."""

from collections.abc import Callable
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from django.core.mail import EmailMessage
from django.db import connection
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.invites import resolve_invite
from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleState, ServiceCycle
from apps.delivery.contracts import Reply
from apps.delivery.models import AutomationPause, MessageState
from apps.tenants.models import (
    FirmMessageState,
    FirmOutboundMessage,
    Invite,
    Membership,
    NotificationPreference,
    TenantRole,
)
from apps.tenants.notification_tasks import dispatch_notice
from apps.tenants.notifications import enqueue_summary, prepare_summary
from apps.tenants.onboarding import FirmRegistration, create_firm
from tests.portal.request_fixtures import portal_setup
from tests.support_threads import run_in_thread_as_runtime
from tests.tenants.test_cancellation_retention import enabled_firm
from tests.ui.factories import add_member

pytestmark = pytest.mark.django_db(transaction=True)


def capture_transport(
    calls: list[EmailMessage], state: str
) -> Callable[[EmailMessage], Reply]:
    def send(email: EmailMessage) -> Reply:
        calls.append(email)
        return Reply(state)

    return send


@pytest.fixture(autouse=True)
def _delivery(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.MEDIA_ROOT = tmp_path
    settings.DELIVERY_PAUSED = False
    monkeypatch.setattr(
        "apps.tenants.notification_tasks.timezone.localdate",
        lambda _value=None: date(2026, 10, 7),
    )


def test_summary_dispatch_commits_before_smtp_and_replay_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firm = enabled_firm("notice-phases")
    with tenant_context(firm.tenant.pk):
        NotificationPreference.objects.create(
            tenant=firm.tenant, user=firm.owner, daily_summary=True
        )
        message = enqueue_summary(firm.owner, firm.tenant, timezone.localdate())
        duplicate = enqueue_summary(firm.owner, firm.tenant, timezone.localdate())
        assert duplicate.pk == message.pk
    observed: list[str] = []

    def send(email: EmailMessage) -> Reply:
        assert not connection.in_atomic_block
        assert email.to == [firm.owner.email]
        observed.append(
            run_in_thread_as_runtime(
                firm.tenant.pk,
                lambda: FirmOutboundMessage.objects.get(pk=message.pk).state,
            )
        )
        return Reply(MessageState.ACCEPTED)

    monkeypatch.setattr("apps.tenants.notification_tasks.smtp_send", send)
    dispatch_notice(firm.tenant.pk, message.pk)
    dispatch_notice(firm.tenant.pk, message.pk)
    assert observed == [FirmMessageState.SENDING]
    with tenant_context(firm.tenant.pk):
        message.refresh_from_db()
        assert message.state == FirmMessageState.ACCEPTED


@pytest.mark.parametrize("revoked", [False, True])
def test_owner_invite_dispatch_uses_a_live_operator_and_a_redeemable_token(
    revoked: bool, settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = User.objects.create_user(
        email="operator@notice-owner.example.com", is_staff=True, is_superuser=True
    )
    tenant = create_firm(
        operator,
        FirmRegistration(
            "Owner invitation", "notice-owner", "owner@invite.example.com"
        ),
    )
    invite = Invite.objects.get(tenant=tenant)
    original_digest = invite.token
    with tenant_context(tenant.pk):
        message = FirmOutboundMessage.objects.get(invite=invite)
    if revoked:
        operator.is_staff = False
        operator.save(update_fields=["is_staff"])
    calls: list[EmailMessage] = []

    def send(email: EmailMessage) -> Reply:
        assert not connection.in_atomic_block
        calls.append(email)
        assert email.to == [invite.email]
        url = next(
            line
            for line in str(email.body).splitlines()
            if line.startswith(settings.PLATFORM_URL)
        )
        token = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
        assert resolve_invite(token).pk == invite.pk
        assert Invite.hash_token(token) != original_digest
        return Reply(MessageState.ACCEPTED)

    monkeypatch.setattr("apps.tenants.notification_tasks.smtp_send", send)
    dispatch_notice(tenant.pk, message.pk)
    with tenant_context(tenant.pk):
        message.refresh_from_db()
        assert message.state == (
            FirmMessageState.CANCELLED if revoked else FirmMessageState.ACCEPTED
        )
    assert len(calls) == (0 if revoked else 1)


@pytest.mark.parametrize("change", ["opt_out", "revoke", "email", "inactive"])
def test_queued_summary_rechecks_preference_recipient_and_membership(
    change: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firm = enabled_firm(f"notice-{change.replace('_', '-')}")
    with tenant_context(firm.tenant.pk):
        preference = NotificationPreference.objects.create(
            tenant=firm.tenant, user=firm.owner, daily_summary=True
        )
        message = enqueue_summary(firm.owner, firm.tenant, timezone.localdate())
        if change == "opt_out":
            preference.daily_summary = False
            preference.save()
        elif change == "revoke":
            Membership.objects.filter(user=firm.owner, tenant=firm.tenant).update(
                is_active=False
            )
        elif change == "email":
            firm.owner.email = "changed@example.com"
            firm.owner.save(update_fields=["email"])
        else:
            firm.owner.is_active = False
            firm.owner.save(update_fields=["is_active"])
    calls: list[EmailMessage] = []
    monkeypatch.setattr(
        "apps.tenants.notification_tasks.smtp_send",
        capture_transport(calls, MessageState.ACCEPTED),
    )
    dispatch_notice(firm.tenant.pk, message.pk)
    assert calls == []
    with tenant_context(firm.tenant.pk):
        message.refresh_from_db()
        assert message.state == FirmMessageState.CANCELLED


@pytest.mark.parametrize("global_pause", [True, False])
def test_global_and_firm_pause_leave_notices_queued(
    global_pause: bool,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firm = enabled_firm("notice-pause")
    settings.DELIVERY_PAUSED = global_pause
    with tenant_context(firm.tenant.pk):
        NotificationPreference.objects.create(
            tenant=firm.tenant, user=firm.owner, daily_summary=True
        )
        message = enqueue_summary(firm.owner, firm.tenant, timezone.localdate())
        if not global_pause:
            AutomationPause.objects.create(tenant=firm.tenant, paused=True)
    calls: list[EmailMessage] = []
    monkeypatch.setattr(
        "apps.tenants.notification_tasks.smtp_send",
        capture_transport(calls, MessageState.ACCEPTED),
    )
    dispatch_notice(firm.tenant.pk, message.pk)
    assert calls == []
    with tenant_context(firm.tenant.pk):
        message.refresh_from_db()
        assert (message.state, message.fence) == (FirmMessageState.QUEUED, 0)


def test_independent_started_receipt_prevents_a_restored_database_from_resending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firm = enabled_firm("notice-restore")
    with tenant_context(firm.tenant.pk):
        NotificationPreference.objects.create(
            tenant=firm.tenant, user=firm.owner, daily_summary=True
        )
        message = enqueue_summary(firm.owner, firm.tenant, timezone.localdate())
    calls: list[EmailMessage] = []
    monkeypatch.setattr(
        "apps.tenants.notification_tasks.smtp_send",
        capture_transport(calls, MessageState.UNKNOWN),
    )
    dispatch_notice(firm.tenant.pk, message.pk)
    with tenant_context(firm.tenant.pk):
        FirmOutboundMessage.objects.filter(pk=message.pk).update(
            state=FirmMessageState.QUEUED, fence=0, lease_until=None
        )
    dispatch_notice(firm.tenant.pk, message.pk)
    assert len(calls) == 1
    with tenant_context(firm.tenant.pk):
        message.refresh_from_db()
        assert message.state == FirmMessageState.UNKNOWN
        assert message.last_error_code == "prior_attempt"


def test_unassigned_cycle_changes_do_not_change_a_staff_summary() -> None:
    portal = portal_setup("notice-private-counts")
    setup = portal.graph
    setup.tenant.features = [*setup.tenant.features, "firm_settings_r1"]
    setup.tenant.save(update_fields=["features"])
    staff = add_member(
        setup.tenant, "unassigned@notices.example.com", TenantRole.STAFF_ACCOUNTANT
    )
    day = date(2026, 10, 7)
    with tenant_context(setup.tenant.pk):
        NotificationPreference.objects.create(
            tenant=setup.tenant, user=staff, daily_summary=True
        )
        message = enqueue_summary(staff, setup.tenant, day)
        before = prepare_summary(message, setup.tenant, on=day)
        ServiceCycle.objects.filter(pk=portal.cycle.pk).update(
            state=CycleState.CANCELLED
        )
        after = prepare_summary(message, setup.tenant, on=day)
        assert before is not None
        assert after is not None
        assert before.body == after.body
