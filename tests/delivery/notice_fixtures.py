"""Local T53 graphs: consent is explicit, never a global tenant fixture default."""

from datetime import timedelta
from pathlib import Path

import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.delivery.models import OutboundMessage, TenantNoticePolicy
from apps.delivery.notice_authority import configure
from apps.delivery.outbox import enqueue
from apps.delivery.policy import BusinessIntent
from apps.tenants.models import Membership, TenantRole
from tests.cycles.cycle_fixtures import PERIOD, CycleSetup, cycle_setup
from tests.support import enrol_totp


@pytest.fixture
def graph(settings: SettingsWrapper, tmp_path: Path) -> CycleSetup:
    """A real issuer with no notice policy unless the test explicitly saves one."""
    settings.DELIVERY_PAUSED = False
    settings.MEDIA_ROOT = tmp_path
    setup = cycle_setup("notice-authority")
    setup.tenant.features = ["cycles_r1", "import_r1"]
    setup.tenant.save(update_fields=["features"])
    enrol_totp(setup.actor)
    return setup


def acknowledge(
    graph: CycleSetup,
    kinds: tuple[str, ...] = ("notice",),
    *,
    actor: User | None = None,
) -> TenantNoticePolicy:
    with tenant_context(graph.tenant.pk):
        policy = TenantNoticePolicy.objects.filter().first()
        return configure(
            actor or graph.actor,
            kinds=kinds,
            acknowledged=True,
            expected_version=policy.version if policy else 0,
        )


def second_owner(graph: CycleSetup) -> User:
    user = User.objects.create_user(email="notice-other-owner@example.test")
    Membership.objects.create(
        user=user, tenant=graph.tenant, role=TenantRole.OWNER, is_active=True
    )
    return user


def manual_notice(
    graph: CycleSetup, *, actor: User | None = None, occurrence: int = 1
) -> OutboundMessage:
    """A genuine implemented standalone notice selects policy, never publication."""
    with tenant_context(graph.tenant.pk):
        contact, _ = ClientContact.objects.get_or_create(
            client=graph.client,
            email="notice-recipient@example.test",
            defaults={"tenant": graph.tenant, "name": "Notice recipient"},
        )
        graph.client.refresh_from_db()
        return enqueue(
            actor or graph.actor,
            graph.client.pk,
            contact.pk,
            intent=BusinessIntent(
                graph.engagement.pk,
                graph.definition.playbook_id,
                PERIOD,
                PERIOD + timedelta(days=29),
                0,
                "owner-notice",
                "notice",
                occurrence,
            ),
            expected_version=graph.client.version,
        )
