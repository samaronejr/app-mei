"""Mutation-sensitive isolation, locks, journals and interrupted sends."""

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import override_settings

from apps.core.tenancy import tenant_context
from apps.delivery import journal
from apps.delivery.gateway import send
from apps.delivery.models import DeliveryAttempt, MessageState, OutboundMessage
from apps.delivery.services import claim_batch
from tests.cycles.cycle_fixtures import cycle_setup
from tests.delivery.conftest import DeliverySetup
from tests.delivery.smtp import FakeSMTP
from tests.support_threads import run_in_thread_as_runtime

pytestmark = pytest.mark.django_db(transaction=True)


def test_skip_locked_does_not_wait_for_another_worker(delivery: DeliverySetup) -> None:
    message = delivery.queue()
    acquired, release = Event(), Event()

    def hold() -> None:
        OutboundMessage.objects.select_for_update().get(pk=message.pk)
        acquired.set()
        assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_in_thread_as_runtime, delivery.graph.tenant.pk, hold)
        try:
            assert acquired.wait(10)
            assert claim_batch(delivery.graph.tenant.pk) == []
        finally:
            release.set()
        future.result(timeout=15)
    assert len(claim_batch(delivery.graph.tenant.pk)) == 1


def test_rls_and_composite_fk_and_control(delivery: DeliverySetup) -> None:
    message = delivery.queue()
    other = cycle_setup("other-delivery")
    with tenant_context(other.tenant.pk):
        # ALL_OBJECTS_OK: exercise database RLS without the manager's filter.
        assert not OutboundMessage.all_objects.filter(pk=message.pk).exists()
        with transaction.atomic(), pytest.raises(IntegrityError):
            DeliveryAttempt.objects.create(
                tenant=other.tenant,
                client=other.client,
                message_id=message.pk,
                outcome="forged",
            )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE app_test")
        # ALL_OBJECTS_OK: vacuity witness, BYPASSRLS sees the foreign row.
        assert OutboundMessage.all_objects.filter(pk=message.pk).exists()


@pytest.mark.parametrize("role", ["app_runtime", "app_test"])
def test_receipts_are_append_only_even_for_table_owner(
    delivery: DeliverySetup, role: str
) -> None:
    delivery.queue()
    with tenant_context(delivery.graph.tenant.pk), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL ROLE {role}")
        with pytest.raises(DatabaseError), transaction.atomic():
            DeliveryAttempt.objects.update(outcome="forged")


@pytest.mark.parametrize("after_send", [False, True])
def test_journal_failure_never_advertises_success_or_retries(
    delivery: DeliverySetup,
    smtp_server: Callable[[str], FakeSMTP],
    monkeypatch: pytest.MonkeyPatch,
    after_send: bool,
) -> None:
    server = smtp_server("accept")
    message = delivery.queue()
    claim = claim_batch(delivery.graph.tenant.pk)[0]
    real_append = journal.append

    def fail(row: OutboundMessage, outcome: str, *, code: str = "") -> str:
        if not after_send or outcome != "intent_started":
            msg = "injected storage interruption"
            raise journal.JournalError(msg)
        return real_append(row, outcome, code=code)

    monkeypatch.setattr(journal, "append", fail)
    with pytest.raises(journal.JournalError):
        send(claim)
    assert len(server.received) == int(after_send)
    assert delivery.reload(message).state == MessageState.SENDING
    assert claim_batch(delivery.graph.tenant.pk) == []
    if after_send:
        with override_settings(DELIVERY_PAUSED=True):
            call_command("reconcile_effects")
        assert delivery.reload(message).state == MessageState.UNKNOWN
        assert claim_batch(delivery.graph.tenant.pk) == []


@pytest.mark.parametrize(
    "data",
    ["not-json", {"schema_version": 9}, {"schema_version": 1, "outcome": "na_fila"}],
)
def test_malformed_journal_fails_closed(delivery: DeliverySetup, data: object) -> None:
    default_storage.save(
        f"effects-journal/{delivery.graph.tenant.pk}/bad.json",
        ContentFile(json.dumps(data).encode()),
    )
    with override_settings(DELIVERY_PAUSED=True), pytest.raises(journal.JournalError):
        call_command("reconcile_effects")


def test_journal_tenant_prefix_cannot_be_spoofed(delivery: DeliverySetup) -> None:
    message = delivery.queue()
    message.lease_owner = uuid4()
    path = journal.append(message, "intent_started")
    with default_storage.open(path) as stream:
        data = json.load(stream)
    data["tenant_id"] = str(uuid4())
    default_storage.save(
        f"effects-journal/{delivery.graph.tenant.pk}/spoof.json",
        ContentFile(json.dumps(data).encode()),
    )
    with override_settings(DELIVERY_PAUSED=True), pytest.raises(journal.JournalError):
        call_command("reconcile_effects")
