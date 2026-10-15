"""Boundary cases found by trying to falsify exact-effect and restore claims."""

from collections.abc import Callable
from uuid import uuid4

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.mail import EmailMessage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.delivery import policy
from apps.delivery.gateway import smtp_send
from apps.delivery.models import DeliveryAttempt, MessageState, OutboundMessage
from apps.delivery.tasks import dispatch_outbox
from tests.browser.harness import mfa_session_cookie
from tests.delivery.conftest import DeliverySetup
from tests.delivery.smtp import FakeSMTP
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)


def test_pipe_error_must_not_be_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    def break_pipe(*args: object, **kwargs: object) -> int:
        raise BrokenPipeError

    monkeypatch.setattr(EmailMessage, "send", break_pipe)
    assert smtp_send(EmailMessage()).state == MessageState.UNKNOWN


def test_rendered_bytes_are_bound_at_egress(
    delivery: DeliverySetup,
    smtp_server: Callable[[str], FakeSMTP],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = smtp_server("accept")
    message = delivery.queue()
    original = policy.render_body
    calls = 0

    def changed(row: OutboundMessage) -> str:
        nonlocal calls
        calls += 1
        return original(row) if calls == 1 else "ALTERED TEMPLATE BYTES"

    monkeypatch.setattr(policy, "render_body", changed)
    dispatch_outbox()
    assert delivery.reload(message).state == MessageState.CANCELLED
    assert message.last_error_code == "content_changed"
    assert not server.received


def test_orphan_journal_tenant_blocks_reconcile(delivery: DeliverySetup) -> None:
    default_storage.save(f"effects-journal/{uuid4()}/orphan.json", ContentFile(b"{}"))
    with (
        override_settings(DELIVERY_PAUSED=True),
        pytest.raises(CommandError, match="scopes"),
    ):
        call_command("reconcile_effects")


def _web(delivery: DeliverySetup, actor: User) -> Client:
    enrol_totp(actor)
    host = f"{delivery.graph.tenant.slug}.localhost"
    web = Client(SERVER_NAME=host)
    cookie = mfa_session_cookie(actor, f"http://{host}/")
    web.cookies[cookie["name"]] = cookie["value"]
    return web


def test_stale_post_cannot_undo_human_decision(delivery: DeliverySetup) -> None:
    message = delivery.queue()
    with tenant_context(delivery.graph.tenant.pk):
        OutboundMessage.objects.filter(pk=message.pk).update(state=MessageState.UNKNOWN)
    web = _web(delivery, delivery.graph.actor)
    data = {
        "message_id": str(message.pk),
        "expected_version": 1,
        "action": "sent",
        "note": "Manual check",
    }
    assert web.get("/comunicacao/").status_code == 200
    assert web.post("/comunicacao/", data).status_code == 302
    data["action"] = "not_sent"
    assert web.post("/comunicacao/", data).status_code == 409
    assert delivery.reload(message).state == MessageState.MANUAL_SENT


def test_unassigned_web_scope_denies_without_any_write(delivery: DeliverySetup) -> None:
    message = delivery.queue()
    web = _web(delivery, delivery.graph.staff)
    assert web.get("/comunicacao/").status_code == 200
    assert str(message.pk).encode() not in web.get("/comunicacao/").content
    with tenant_context(delivery.graph.tenant.pk):
        before = DeliveryAttempt.objects.count()
    assert (
        web.post(
            "/comunicacao/",
            {"message_id": str(message.pk), "expected_version": 1, "action": "cancel"},
        ).status_code
        == 404
    )
    with tenant_context(delivery.graph.tenant.pk):
        assert DeliveryAttempt.objects.count() == before


def test_flag_off_http_surface_is_absent(delivery: DeliverySetup) -> None:
    delivery.graph.tenant.features = []
    delivery.graph.tenant.save(update_fields=["features"])
    assert _web(delivery, delivery.graph.actor).get("/comunicacao/").status_code == 404
