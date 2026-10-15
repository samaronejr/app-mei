"""Runtime SQL cannot alter an intent's authority or silently carry consent."""

import json
from uuid import uuid4

import pytest
from django.db import IntegrityError, connection, transaction

from apps.core.tenancy import tenant_context
from apps.delivery.models import OutboundMessage, TenantNoticePolicy
from apps.delivery.notice_authority import configure, validate_notice
from tests.cycles.cycle_fixtures import CycleSetup
from tests.delivery import notice_fixtures
from tests.delivery.notice_fixtures import acknowledge, manual_notice

pytestmark = pytest.mark.django_db(transaction=True)
graph = notice_fixtures.graph


@pytest.mark.parametrize(
    "field",
    [
        "notice_authorization_bytes",
        "notice_authorization_digest",
        "kind",
        "tenant_id",
        "client_id",
        "contact_id",
        "invite_id",
        "cycle_id",
        "evidence_request_id",
        "created_by_id",
        "business_intent_key",
        "retry_of_id",
        "content_digest",
    ],
)
def test_bound_authority_and_identity_are_immutable(
    graph: CycleSetup, field: str
) -> None:
    acknowledge(graph)
    message = manual_notice(graph)
    value: object
    if field == "notice_authorization_bytes":
        value = None
    elif field == "notice_authorization_digest":
        value = b"\x00" * 32
    elif field == "kind":
        value = "approved_email"
    elif field.endswith("_id"):
        value = uuid4()
    else:
        value = "f" * 64
    with tenant_context(graph.tenant.pk):
        with pytest.raises(IntegrityError) as error, transaction.atomic():
            OutboundMessage.objects.filter(pk=message.pk).update(**{field: value})
        assert getattr(error.value.__cause__, "sqlstate", None) == "23514"
        stored = OutboundMessage.objects.get(pk=message.pk)
        assert stored.notice_authorization_bytes == message.notice_authorization_bytes
        assert stored.notice_authorization_digest == message.notice_authorization_digest
        assert validate_notice(stored) == (True, "authorized")


def test_new_templated_insert_cannot_omit_authority(graph: CycleSetup) -> None:
    acknowledge(graph)
    message = manual_notice(graph)
    with tenant_context(graph.tenant.pk), connection.cursor() as cursor:
        with pytest.raises(IntegrityError) as error, transaction.atomic():
            cursor.execute(
                "INSERT INTO delivery_outboundmessage "
                "(id, tenant_id, client_id, contact_id, kind, "
                "business_intent_key, idempotency_key, content_digest, state, "
                "recipients_json, subject, body_ref, portal_url, attempts, "
                "fence, version, next_attempt_at, created_at) "
                "SELECT %s, tenant_id, client_id, contact_id, kind, %s, %s, "
                "content_digest, state, recipients_json, subject, body_ref, "
                "portal_url, attempts, fence, version, next_attempt_at, created_at "
                "FROM delivery_outboundmessage WHERE id=%s",
                [uuid4(), "d" * 64, "c" * 64, message.pk],
            )
        assert getattr(error.value.__cause__, "sqlstate", None) == "23514"
        assert OutboundMessage.objects.count() == 1


def test_policy_grant_mutation_cannot_retain_old_acknowledgement(
    graph: CycleSetup,
) -> None:
    policy = acknowledge(graph)
    with tenant_context(graph.tenant.pk):
        changed = [{"kind": "portal_invite", "mode": "manual", "rule_version": None}]
        with pytest.raises(IntegrityError), transaction.atomic():
            TenantNoticePolicy.objects.filter(pk=policy.pk).update(grants_json=changed)
        with pytest.raises(IntegrityError), transaction.atomic():
            TenantNoticePolicy.objects.filter(pk=policy.pk).update(
                grants_json=changed, version=2
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            TenantNoticePolicy.objects.filter(pk=policy.pk).delete()
        policy.refresh_from_db()
        assert policy.version == policy.acknowledged_version == 1
        assert policy.grants_json == [
            {"kind": "notice", "mode": "manual", "rule_version": None}
        ]


def test_reacknowledgement_invalidates_old_intent_without_changing_key(
    graph: CycleSetup,
) -> None:
    acknowledge(graph)
    message = manual_notice(graph)
    assert message.notice_authorization_bytes is not None
    old_bytes = bytes(message.notice_authorization_bytes)
    old_key = message.business_intent_key
    acknowledge(graph)
    with tenant_context(graph.tenant.pk):
        assert validate_notice(message) == (False, "notice_policy_changed")
        message.refresh_from_db()
        assert message.notice_authorization_bytes is not None
        assert bytes(message.notice_authorization_bytes) == old_bytes
        assert message.business_intent_key == old_key
        assert json.loads(old_bytes)["basis"]["policy_version"] == 1
        configure(graph.actor, kinds=(), acknowledged=False, expected_version=2)
        assert validate_notice(message)[0] is False
