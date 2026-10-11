"""Exact grants, genuine owner acknowledgement and independent published basis."""

import json
from dataclasses import replace
from datetime import timedelta
from http import HTTPStatus
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import Event
from apps.authz.support import SupportContext, current_support
from apps.core.tenancy import tenant_context
from apps.delivery.models import OutboundMessage, TenantNoticePolicy
from apps.delivery.notice_authority import configure, validate_notice
from apps.delivery.outbox import enqueue
from apps.tenants.models import Membership
from tests.cycles.cycle_fixtures import CycleSetup, cycle_setup
from tests.delivery import notice_fixtures
from tests.delivery.conftest import DeliverySetup
from tests.delivery.notice_fixtures import acknowledge, manual_notice, second_owner
from tests.delivery.test_portal_invite_outbox import _contact, _firm
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)
graph = notice_fixtures.graph


def test_get_never_creates_or_acknowledges_policy(graph: CycleSetup) -> None:
    response = _firm(graph).get(reverse("notice-policy"))
    assert response.status_code == HTTPStatus.OK
    assert response.context_data is not None
    assert response.context_data["policy_state"] == "absent"
    with tenant_context(graph.tenant.pk):
        assert not TenantNoticePolicy.objects.exists()


@pytest.mark.parametrize("features", [["import_r1"], ["cycles_r1", "import_r1"]])
def test_owner_save_readback_revoke_and_pause_are_live(
    graph: CycleSetup, features: list[str]
) -> None:
    graph.tenant.features = features
    graph.tenant.save(update_fields=["features"])
    browser = _firm(graph)
    url = reverse("notice-policy")
    assert (
        browser.post(
            url, {"action": "save", "expected_version": 0, "kinds": ["notice"]}
        ).status_code
        == HTTPStatus.UNPROCESSABLE_ENTITY
    )
    with tenant_context(graph.tenant.pk):
        assert not TenantNoticePolicy.objects.exists()
    assert (
        browser.post(
            url,
            {
                "action": "save",
                "expected_version": 0,
                "kinds": ["notice"],
                "acknowledged": "on",
            },
        ).status_code
        == HTTPStatus.FOUND
    )
    with tenant_context(graph.tenant.pk):
        policy = TenantNoticePolicy.objects.get()
        owner_seat = Membership.objects.get(
            user=graph.actor, tenant=graph.tenant, client=None
        )
        assert policy.version == policy.acknowledged_version == 1
        assert policy.acknowledged_by_id == graph.actor.pk
        assert policy.ack_membership_id == owner_seat.pk
        assert policy.ack_membership_version == owner_seat.version
        assert policy.grants_json == [
            {"kind": "notice", "mode": "manual", "rule_version": None}
        ]
        acknowledged_event = Event.objects.get(action="delivery.notice_policy_changed")
        assert policy.acknowledged_at is not None
        acknowledgement = {
            "acknowledged_by_id": str(graph.actor.pk),
            "ack_membership_id": str(owner_seat.pk),
            "ack_membership_version": owner_seat.version,
            "acknowledgement_code": policy.acknowledgement_code,
            "acknowledged_at": policy.acknowledged_at.isoformat(),
        }
        assert {
            key: acknowledged_event.metadata.get(key) for key in acknowledgement
        } == acknowledgement
    assert (
        browser.post(url, {"action": "revoke", "expected_version": 0}).status_code
        == HTTPStatus.CONFLICT
    )
    assert (
        browser.post(url, {"action": "revoke", "expected_version": 1}).status_code
        == HTTPStatus.FOUND
    )
    assert (
        browser.post(url, {"action": "pause", "expected_version": 1}).status_code
        == HTTPStatus.FOUND
    )
    assert (
        browser.post(url, {"action": "resume", "expected_version": 1}).status_code
        == HTTPStatus.CONFLICT
    )
    with tenant_context(graph.tenant.pk):
        policy.refresh_from_db()
        assert policy.version == 2
        assert policy.grants_json == []
        assert policy.acknowledged_by_id is None
        assert policy.acknowledged_version is None
        acknowledged_event.refresh_from_db()
        assert {
            key: acknowledged_event.metadata.get(key) for key in acknowledgement
        } == acknowledgement


@pytest.mark.parametrize(
    "kinds",
    [["approved_email"], ["contact_portal_invite"], ["unknown"], ["notice", "notice"]],
)
def test_unknown_consequential_and_unimplemented_grants_refuse(
    graph: CycleSetup, kinds: list[str]
) -> None:
    response = _firm(graph).post(
        reverse("notice-policy"),
        {"action": "save", "expected_version": 0, "kinds": kinds, "acknowledged": "on"},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    with tenant_context(graph.tenant.pk):
        assert not TenantNoticePolicy.objects.exists()


@pytest.mark.parametrize(
    "identity", ["non_owner", "superuser", "wrong_tenant", "support"]
)
def test_only_current_ordinary_owner_can_acknowledge(
    graph: CycleSetup, identity: str
) -> None:
    actor = graph.staff if identity == "non_owner" else graph.actor
    tid = graph.tenant.pk
    token = None
    if identity == "superuser":
        actor.is_superuser = True
        actor.save(update_fields=["is_superuser"])
    elif identity == "wrong_tenant":
        tid = cycle_setup("notice-foreign").tenant.pk
    elif identity == "support":
        token = current_support.set(
            SupportContext(
                uuid4(),
                tid,
                "read_only",
                frozenset(),
                timezone.now() + timedelta(hours=1),
            )
        )
    try:
        with tenant_context(tid), pytest.raises(PermissionDenied):
            configure(actor, kinds=("notice",), acknowledged=True, expected_version=0)
    finally:
        if token is not None:
            current_support.reset(token)
    with tenant_context(graph.tenant.pk):
        assert not TenantNoticePolicy.objects.exists()


def test_private_owner_page_denies_non_owner_before_reading_policy(
    graph: CycleSetup,
) -> None:
    acknowledge(graph)
    enrol_totp(graph.staff)
    browser = _firm(replace(graph, actor=graph.staff))
    assert browser.get(reverse("notice-policy")).status_code == HTTPStatus.FORBIDDEN
    assert (
        browser.post(
            reverse("notice-policy"), {"action": "revoke", "expected_version": 1}
        ).status_code
        == HTTPStatus.FORBIDDEN
    )
    with tenant_context(graph.tenant.pk):
        assert TenantNoticePolicy.objects.get().version == 1


def test_owner_page_reports_acknowledgement_lifetime_without_refreshing_it(
    graph: CycleSetup,
) -> None:
    owner = second_owner(graph)
    acknowledge(graph, actor=owner)
    browser = _firm(graph)
    response = browser.get(reverse("notice-policy"))
    assert response.context_data is not None
    assert response.context_data["policy_state"] == "authorized"
    member = Membership.objects.get(user=owner, tenant=graph.tenant, client=None)
    member.is_active = False
    member.save(update_fields=["is_active"])
    member.is_active = True
    member.save(update_fields=["is_active"])
    response = browser.get(reverse("notice-policy"))
    assert response.context_data is not None
    assert response.context_data["policy_state"] == "stale"
    with tenant_context(graph.tenant.pk):
        assert TenantNoticePolicy.objects.get().version == 1


@pytest.mark.parametrize("state", ["missing", "unacknowledged", "other_kind"])
def test_standalone_notice_needs_its_exact_current_acknowledgement(
    graph: CycleSetup, state: str
) -> None:
    with tenant_context(graph.tenant.pk):
        if state == "unacknowledged":
            TenantNoticePolicy.objects.create(
                tenant=graph.tenant,
                grants_json=[
                    {"kind": "notice", "mode": "manual", "rule_version": None}
                ],
            )
        elif state == "other_kind":
            acknowledge(graph, ("portal_invite",))
        with pytest.raises(ValidationError):
            manual_notice(graph)
        assert not OutboundMessage.objects.exists()


def test_policy_binding_uses_real_owner_seat_and_exact_grant(graph: CycleSetup) -> None:
    owner = second_owner(graph)
    policy = acknowledge(graph, actor=owner)
    message = manual_notice(graph)
    assert message.notice_authorization_bytes is not None
    binding = json.loads(bytes(message.notice_authorization_bytes))
    assert binding["issuer_id"] == str(graph.actor.pk)
    assert binding["basis"]["owner_id"] == str(owner.pk)
    assert binding["basis"]["policy_id"] == str(policy.pk)
    assert binding["basis"]["policy_version"] == policy.version
    assert binding["kind"] == "notice"
    assert binding["mode"] == "manual"
    with tenant_context(graph.tenant.pk):
        assert validate_notice(message) == (True, "authorized")


def test_published_cycle_does_not_need_any_policy_row(delivery: DeliverySetup) -> None:
    message = delivery.queue()
    assert message.notice_authorization_bytes is not None
    binding = json.loads(bytes(message.notice_authorization_bytes))
    assert binding["basis"]["type"] == "playbook"
    assert binding["basis"]["playbook_version_id"] == str(
        delivery.cycle.playbook_version_id
    )
    with tenant_context(delivery.graph.tenant.pk):
        assert not TenantNoticePolicy.objects.exists()
        assert validate_notice(message) == (True, "authorized")


def test_foreign_cycle_is_not_a_policy_fallback(
    graph: CycleSetup, delivery: DeliverySetup
) -> None:
    acknowledge(graph)
    contact = _contact(graph)
    with tenant_context(graph.tenant.pk), pytest.raises(ValidationError):
        enqueue(
            graph.actor,
            graph.client.pk,
            contact.pk,
            intent=replace(delivery.intent, occurrence=3),
            expected_version=graph.client.version,
            cycle_id=delivery.cycle.pk,
        )
