"""Crash windows and individual attempt accounting beyond the initial restore."""

from dataclasses import replace
from datetime import timedelta
from typing import Literal
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.integrations import (
    call_contracts,
    call_protocol,
    firm_calls,
    provider_journal,
)
from apps.integrations.call_contracts import CallResult, Operation, StaleClaimError
from apps.integrations.call_models import CallState, ProviderCall, ProviderCallAttempt
from apps.integrations.call_protocol import (
    begin_io,
    claim,
    finalize,
    perform,
    record_result,
    reserve,
)
from apps.integrations.call_state import current_epoch
from apps.integrations.models import ClientProviderLink
from apps.integrations.provider_journal import JournalError
from apps.tenants.models import UsageKind, UsageRecord
from tests.integrations.provider_peer import ProviderPeer
from tests.integrations.test_call_protocol import (
    SyntheticPolicy,
    provider_link,
    reload_call,
    request,
)
from tests.integrations.test_provider_restore import restore_before_call

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["provider_link"]


def test_expired_finalize_does_not_advertise_success(
    provider_link: ClientProviderLink,
) -> None:
    # Given a live claim whose lease expires while a real response is in flight.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None

    class ExpiringPeer(ProviderPeer):
        def send(
            self, dispatch: call_protocol.Dispatch | firm_calls.FirmDispatch
        ) -> CallResult:
            result = super().send(dispatch)
            with tenant_context(provider_link.tenant_id):
                ProviderCall.objects.filter(pk=row.pk).update(
                    lease_until=timezone.now() - timedelta(seconds=1)
                )
            return result

    # When the fenced finalize no longer owns a live lease.
    with ExpiringPeer() as peer:
        result = perform(ticket, peer)
    # Then the public result stays uncertain; the independent receipt permits recovery.
    assert result.state == CallState.UNCERTAIN
    assert reload_call(provider_link, row.pk).state == CallState.UNCERTAIN
    assert peer.requests == [ticket.owner.hex]


@pytest.mark.parametrize("quarantine", ["another_claim", "own_finalize"])
def test_reply_after_quarantine_stays_late_until_reconcile_settles_it_once(
    provider_link: ClientProviderLink, quarantine: str
) -> None:
    # Given a started billed attempt whose lease expired while its reply was in flight.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    dispatch = begin_io(ticket)
    with tenant_context(provider_link.tenant_id):
        ProviderCall.objects.filter(pk=row.pk).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
    if quarantine == "another_claim":
        assert claim(provider_link.tenant_id, row.pk) is None
    reply = CallResult(CallState.SUCCESS, 200, "ok", "sim")
    record_result(dispatch, reply)
    policy = call_contracts.operation_for("SP-DAS").policy
    assert isinstance(policy, SyntheticPolicy)
    # When the original worker reports its current-fence reply.
    assert not finalize(ticket, reply)
    # Then the reply is kept and metered as late, but not settled or applied.
    assert reload_call(provider_link, row.pk).state == CallState.UNCERTAIN
    with tenant_context(provider_link.tenant_id):
        assert list(
            ProviderCallAttempt.objects.filter(call_id=row.pk).values_list(
                "observation", "outcome"
            )
        ) == [("late", CallState.SUCCESS)]
        assert UsageRecord.objects.filter(source_event=ticket.owner).count() == 1
    assert policy.settled == []
    # When reconciliation reads the independent result receipt.
    with override_settings(PROVIDERS_PAUSED=True):
        call_command("reconcile_provider_operations", restore_ref=str(uuid4()))
    # Then the journaled result is applied and settled exactly once.
    assert reload_call(provider_link, row.pk).state == CallState.SUCCESS
    assert policy.settled == ["sim"]
    with tenant_context(provider_link.tenant_id):
        assert UsageRecord.objects.filter(source_event=ticket.owner).count() == 1


@pytest.mark.parametrize("billing_class", ["nao_faturada", "incluido_no_plano"])
def test_unbilled_read_keeps_fencing_but_skips_journal_and_reservation(
    provider_link: ClientProviderLink,
    billing_class: Literal["nao_faturada", "incluido_no_plano"],
) -> None:
    # Given an explicitly unbilled synthetic read and a policy that forbids reserving.
    class FreeReadPolicy(SyntheticPolicy):
        def reserve(self, call: ProviderCall) -> call_contracts.Reservation:
            pytest.fail("An unbilled read must not reserve budget")

    call_contracts.register_operation(
        Operation(
            "SYN-READ",
            "serpro",
            "v1",
            "read",
            "federal_checks",
            billing_class,
            FreeReadPolicy(),
        )
    )
    row = reserve(
        provider_link.tenant_id,
        provider_link.pk,
        replace(request(), operation="SYN-READ"),
    )
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    # When the unbilled read is dispatched and finalized.
    dispatch = begin_io(ticket)
    result = CallResult(CallState.SUCCESS, 200, "ok", "nao")
    record_result(dispatch, result)
    assert finalize(ticket, result)
    # Then it has no independent billing receipt, but its fence cannot start twice.
    assert row.reserved_microbrl == 0
    assert provider_journal.read(provider_link.tenant_id) == []
    with pytest.raises(StaleClaimError):
        begin_io(ticket)


def test_result_after_lost_finalize_is_recovered_without_repeating_wire(
    provider_link: ClientProviderLink,
) -> None:
    # Given an independent response receipt but a lost finalize transaction.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    dispatch = begin_io(ticket)
    with ProviderPeer() as peer:
        result = peer.send(dispatch)
        record_result(dispatch, result)
        # When recovery runs with no finalized database outcome.
        with override_settings(PROVIDERS_PAUSED=True):
            call_command("reconcile_provider_operations", restore_ref=str(uuid4()))
        # Then the recorded result is applied and cannot be sent again.
        assert reload_call(provider_link, row.pk).state == CallState.SUCCESS
        assert claim(provider_link.tenant_id, row.pk) is None
        assert peer.requests == [ticket.owner.hex]


def test_independent_epoch_survives_restores_without_any_started_calls(
    provider_link: ClientProviderLink,
) -> None:
    # Given a claim from an epoch newer than the restored database.
    with override_settings(PROVIDERS_PAUSED=True):
        call_command("reconcile_provider_operations", restore_ref=str(uuid4()))
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        try:
            cursor.execute("UPDATE integrations_providerexecutionepoch SET epoch=1")
        finally:
            cursor.execute("SET ROLE app_runtime")
    # When the older database is recovered again, with no call journal to set a floor.
    with override_settings(PROVIDERS_PAUSED=True):
        call_command("reconcile_provider_operations", restore_ref=str(uuid4()))
    # Then the independent restore receipt prevents epoch reuse.
    assert current_epoch() > ticket.execution_epoch
    with pytest.raises(StaleClaimError):
        begin_io(ticket)


def test_journal_write_failure_prevents_any_external_request(
    provider_link: ClientProviderLink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a prepared claim and unavailable independent storage.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None

    def unavailable(_record: provider_journal.JournalRecord) -> str:
        raise JournalError

    monkeypatch.setattr(provider_journal, "append", unavailable)
    # When dispatch cannot persist its first receipt.
    with ProviderPeer() as peer, pytest.raises(JournalError):
        perform(ticket, peer)
    # Then neither the wire nor a repeated ticket can start.
    assert peer.requests == []
    with pytest.raises(StaleClaimError):
        begin_io(ticket)


def test_same_claim_with_wrong_client_is_refused(
    provider_link: ClientProviderLink,
) -> None:
    # Given a legitimate ticket whose client dimension is corrupted.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    # When the worker presents the wrong client identity.
    with pytest.raises(ProviderCall.DoesNotExist):
        begin_io(replace(ticket, client_id=uuid4()))
    # Then the real call is still only claimed.
    assert reload_call(provider_link, row.pk).state == CallState.CLAIMED


def test_every_read_attempt_and_its_usage_survive_restore(
    provider_link: ClientProviderLink,
) -> None:
    # Given a billed read with one definite temporary reply and one success.
    call_contracts.register_operation(
        Operation(
            "SP-PAG",
            "serpro",
            "v1",
            "read",
            "federal_checks",
            "consulta",
            SyntheticPolicy(),
        )
    )
    row = reserve(
        provider_link.tenant_id,
        provider_link.pk,
        replace(request(), operation="SP-PAG"),
    )
    first = claim(provider_link.tenant_id, row.pk)
    assert first is not None
    dispatch = begin_io(first)
    retry = CallResult(CallState.RETRY, 503, "temporarily_unavailable", "sim")
    record_result(dispatch, retry)
    assert finalize(first, retry)
    assert claim(provider_link.tenant_id, row.pk) is None
    with tenant_context(provider_link.tenant_id):
        ProviderCall.objects.filter(pk=row.pk).update(
            next_attempt_at=timezone.now() - timedelta(seconds=1)
        )
    second = claim(provider_link.tenant_id, row.pk)
    assert second is not None
    with ProviderPeer() as peer:
        perform(second, peer)
        restore_before_call()
        # When recovery rebuilds the pre-call horizon.
        with override_settings(PROVIDERS_PAUSED=True):
            call_command("reconcile_provider_operations", restore_ref=str(uuid4()))
        # Then neither an older success nor aggregate precedence loses an attempt.
        restored = reload_call(provider_link, row.pk)
        assert restored.attempts == 2
        assert restored.state == CallState.SUCCESS
        with tenant_context(provider_link.tenant_id):
            assert ProviderCallAttempt.objects.filter(call=restored).count() == 2
            assert set(
                UsageRecord.objects.filter(metric="serpro_consulta").values_list(
                    "source_event", flat=True
                )
            ) == {first.owner, second.owner}
        assert peer.requests == [second.owner.hex]


def test_late_result_corrects_estimated_usage_without_rewriting_it(
    provider_link: ClientProviderLink,
) -> None:
    # Given start-only uncertainty already recovered into estimated exposure.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    dispatch = begin_io(ticket)
    with override_settings(PROVIDERS_PAUSED=True):
        call_command("reconcile_provider_operations", restore_ref=str(uuid4()))
    with tenant_context(provider_link.tenant_id):
        original = UsageRecord.objects.get(source_event=ticket.owner)
        assert original.kind == UsageKind.ESTIMATED
    # When a durable, definite unbilled result arrives and recovery is repeated.
    record_result(dispatch, CallResult(CallState.DENIED, 403, "denied_unbilled", "nao"))
    with override_settings(PROVIDERS_PAUSED=True):
        for _ in range(2):
            call_command("reconcile_provider_operations", restore_ref=str(uuid4()))
    # Then a single correction retains the original immutable exposure receipt.
    with tenant_context(provider_link.tenant_id):
        original.refresh_from_db()
        assert original.quantity == 1
        correction = UsageRecord.objects.get(correction_of=original)
        assert correction.quantity == 0
        assert correction.kind == UsageKind.CONFIRMED
