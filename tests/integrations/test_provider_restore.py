"""Restore replay must recover each attempt without another provider request."""

from uuid import uuid4

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test import override_settings

from apps.core.tenancy import tenant_context
from apps.integrations import call_protocol, provider_journal
from apps.integrations.call_contracts import CallResult, StaleClaimError
from apps.integrations.call_models import (
    CallState,
    ProviderCall,
    ProviderCallAttempt,
    ProviderExecutionEpoch,
)
from apps.integrations.call_protocol import begin_io, claim, record_result, reserve
from apps.integrations.models import ClientProviderLink, IntegrationIssue
from apps.integrations.provider_journal import JournalError
from apps.tenants.models import UsageRecord
from tests.integrations.provider_peer import ProviderPeer
from tests.integrations.test_call_protocol import (
    provider_link,
    reload_call,
    request,
)

pytestmark = pytest.mark.django_db(transaction=True)

__all__ = ["provider_link"]


def test_start_only_journal_becomes_uncertain_with_owned_work(
    provider_link: ClientProviderLink,
) -> None:
    # Given a committed independent start but no response.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    begin_io(ticket)
    with tenant_context(provider_link.tenant_id):
        ProviderCall.objects.filter(pk=row.pk).update(
            state=CallState.RESERVED, attempts=0, fence=0
        )
    # When a restored database is reconciled under the global pause.
    with override_settings(PROVIDERS_PAUSED=True):
        call_command("reconcile_provider_operations", restore_ref=str(uuid4()))
    # Then the restored queue cannot start this effect again.
    assert reload_call(provider_link, row.pk).state == CallState.UNCERTAIN
    assert claim(provider_link.tenant_id, row.pk) is None
    with tenant_context(provider_link.tenant_id):
        assert IntegrationIssue.objects.filter(
            client_id=provider_link.client_id,
            reason="chamada_provedor_incerta",
            state="aberta",
        ).exists()


def test_reconcile_refuses_without_the_global_pause(
    provider_link: ClientProviderLink,
) -> None:
    # Given providers are enabled, even when no calls exist.
    # When the operator attempts restore replay.
    with pytest.raises(CommandError, match="PROVIDERS_PAUSED"):
        call_command("reconcile_provider_operations", restore_ref=str(uuid4()))
    # Then the execution epoch remains unchanged.
    assert ProviderExecutionEpoch.objects.get(pk=1).epoch == 1


def test_restore_epoch_refuses_an_old_queued_claim(
    provider_link: ClientProviderLink,
) -> None:
    # Given a queued Celery message carrying the old execution epoch.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    with override_settings(PROVIDERS_PAUSED=True):
        call_command("reconcile_provider_operations", restore_ref=str(uuid4()))
    # When it is delivered after the restore procedure has advanced the epoch.
    with pytest.raises(StaleClaimError):
        begin_io(ticket)
    # Then no dispatch start was committed.
    assert reload_call(provider_link, row.pk).dispatch_started_at is None


def test_journal_and_network_boundaries_refuse_a_tenant_transaction(
    provider_link: ClientProviderLink,
) -> None:
    # Given a valid committed claim.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    # When an eager task wrongly encloses dispatch in a tenant transaction.
    with (
        tenant_context(provider_link.tenant_id),
        pytest.raises(RuntimeError, match="External I/O"),
    ):
        begin_io(ticket)


def test_result_journal_also_refuses_an_open_transaction(
    provider_link: ClientProviderLink,
) -> None:
    # Given a started call whose response arrived outside the DB.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    dispatch = begin_io(ticket)
    # When a caller tries to persist its independent receipt inside a transaction.
    with transaction.atomic(), pytest.raises(RuntimeError, match="External I/O"):
        record_result(dispatch, CallResult(CallState.SUCCESS, 200, "ok", "sim"))


def test_portal_has_no_select_grant_on_the_platform_epoch(
    provider_link: ClientProviderLink,
) -> None:
    # Given the platform singleton, not a tenant-scoped business table.
    # When portal privileges are inspected independently of ORM filtering.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT has_table_privilege('app_portal', "
            "'integrations_providerexecutionepoch', 'SELECT')"
        )
        # Then an accidentally unscoped portal read is denied at the SQL boundary.
        assert cursor.fetchone() == (False,)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema_version":1',
        b'{"schema_version":999}',
        b'{"schema_version":1,"tenant_id":"not-a-uuid"}',
        b'{"schema_version":1,"response_body":"synthetic-canary"}',
    ],
)
def test_malformed_or_untrusted_journal_cannot_report_success(
    provider_link: ClientProviderLink, payload: bytes
) -> None:
    # Given an independent object that is not a valid minimized protocol receipt.
    default_storage.save(
        f"provider-journal/{provider_link.tenant_id}/2026/10/hostile.json",
        ContentFile(payload),
    )
    # When restore reads the independent prefix.
    with pytest.raises(JournalError):
        provider_journal.read(provider_link.tenant_id)
    # Then malformed input cannot be mistaken for an empty, successful journal.


def test_independent_journal_read_refuses_a_tenant_transaction(
    provider_link: ClientProviderLink,
) -> None:
    # Given an enclosing tenant transaction which must not hold external I/O.
    # When a caller tries to inventory or read independent objects.
    with (
        tenant_context(provider_link.tenant_id),
        pytest.raises(RuntimeError, match="External I/O"),
    ):
        provider_journal.read(provider_link.tenant_id)


def test_runtime_cannot_increment_the_epoch_directly(
    provider_link: ClientProviderLink,
) -> None:
    # Given app_runtime, which is not the restore procedure's schema owner.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT has_table_privilege('app_runtime', "
            "'integrations_providerexecutionepoch', 'UPDATE')"
        )
        # Then ordinary workers cannot advance the platform epoch themselves.
        assert cursor.fetchone() == (False,)


def test_repeated_recovery_cannot_reclaim_a_started_effect(
    provider_link: ClientProviderLink,
) -> None:
    # Given an interrupted call with an independent intent_started receipt.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    begin_io(ticket)
    # When the operator repeats recovery after further interruptions.
    with override_settings(PROVIDERS_PAUSED=True):
        for _ in range(3):
            call_command("reconcile_provider_operations", restore_ref=str(uuid4()))
    # Then uncertainty persists and no repeated dispatch is admitted.
    assert reload_call(provider_link, row.pk).state == CallState.UNCERTAIN
    assert claim(provider_link.tenant_id, row.pk) is None


def restore_before_call() -> None:
    """Restore the pre-call row horizon, keeping independent object storage intact."""
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        try:
            cursor.execute(
                "TRUNCATE integrations_providereffectbarrier, "
                "integrations_providercallattempt, "
                "integrations_providercall, tenants_usagerecord"
            )
            # The issue table is AXRM-008's: drop only call-uncertainty work items.
            cursor.execute(
                "DELETE FROM integrations_integrationissue "
                "WHERE reason = 'chamada_provedor_incerta'"
            )
        finally:
            cursor.execute("SET ROLE app_runtime")


def test_restore_reinserts_final_call_and_usage_without_another_request(
    provider_link: ClientProviderLink,
) -> None:
    # Given one wire-confirmed billed call, newer than the restored DB horizon.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    with ProviderPeer() as peer:
        reply = call_protocol.perform(ticket, peer)
        assert reply.state == CallState.SUCCESS
        assert peer.requests == [ticket.owner.hex]
        restore_before_call()
        # When the independent journal is reconciled, twice to exercise idempotency.
        with override_settings(PROVIDERS_PAUSED=True):
            for _ in range(2):
                call_command("reconcile_provider_operations", restore_ref=str(uuid4()))
        # Then the exact original call and per-attempt usage are restored.
        restored = reload_call(provider_link, row.pk)
        assert restored.state == CallState.SUCCESS
        assert restored.attempts == 1
        with tenant_context(provider_link.tenant_id):
            assert ProviderCallAttempt.objects.filter(call=restored).count() == 1
            assert (
                UsageRecord.objects.get(
                    metric="serpro_emissao", source_event=ticket.owner
                ).quantity
                == 1
            )
        repeated = reserve(provider_link.tenant_id, provider_link.pk, request())
        assert repeated.pk == row.pk
        assert claim(provider_link.tenant_id, row.pk) is None
        assert peer.requests == [ticket.owner.hex]


def test_reset_after_request_is_uncertain_and_never_retried(
    provider_link: ClientProviderLink,
) -> None:
    # Given a peer that receives the request but resets before its response.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    with ProviderPeer(reset=True) as peer:
        # When the only transmission is interrupted at the ambiguous boundary.
        reply = call_protocol.perform(ticket, peer)
        # Then an owned uncertainty, not a retry, records what the wire proved.
        assert reply.state == CallState.UNCERTAIN
        assert peer.received.is_set()
        assert peer.requests == [ticket.owner.hex]
        assert claim(provider_link.tenant_id, row.pk) is None
        assert claim(provider_link.tenant_id, row.pk) is None
        assert peer.requests == [ticket.owner.hex]
        with tenant_context(provider_link.tenant_id):
            issue = IntegrationIssue.objects.get(
                reason="chamada_provedor_incerta", state="aberta"
            )
            assert issue.owner_id is not None
