"""Restore must retain attempt identity and reject an incomplete journal horizon."""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.integrations import call_contracts, provider_journal
from apps.integrations.call_contracts import CallResult
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_protocol import (
    begin_io,
    claim,
    finalize,
    perform,
    record_result,
    reserve,
)
from apps.integrations.models import ClientProviderLink
from tests.integrations.provider_peer import ProviderPeer
from tests.integrations.test_call_protocol import (
    provider_link,
    reload_call,
    request,
)
from tests.integrations.test_provider_restore import restore_before_call

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["provider_link"]


def test_replay_settlement_receives_original_attempt_identity(
    provider_link: ClientProviderLink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an accounted request whose call row is newer than the restored database.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    with ProviderPeer() as peer:
        perform(ticket, peer)
    restore_before_call()
    policy = call_contracts.operation_for("SP-DAS").policy
    original = policy.settle
    observed: list[UUID | None] = []

    def settled(call: ProviderCall, result: CallResult) -> None:
        observed.append(call.x_request_tag)
        original(call, result)

    monkeypatch.setattr(policy, "settle", settled)
    # When the independent attempt is restored and settled.
    with override_settings(PROVIDERS_PAUSED=True):
        call_command("reconcile_provider_operations", restore_ref=str(uuid4()))
    # Then the hook identifies the original attempt, not a missing/latest identity.
    assert observed == [ticket.owner]


def test_missing_independent_result_cannot_regress_a_finalized_call(
    provider_link: ClientProviderLink,
) -> None:
    # Given a finalized call but an incomplete independent storage restore.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    with ProviderPeer() as peer:
        perform(ticket, peer)
    result = next(
        record
        for record in provider_journal.read(provider_link.tenant_id)
        if record.result is not None
    )
    default_storage.delete(result.path)
    # When reconciliation encounters the missing independent result receipt.
    with override_settings(PROVIDERS_PAUSED=True), pytest.raises(CommandError):
        call_command("reconcile_provider_operations", restore_ref=str(uuid4()))
    # Then recovery fails closed rather than regressing finalized evidence.
    assert reload_call(provider_link, row.pk).state == CallState.SUCCESS


def test_missing_earlier_attempt_cannot_report_complete_recovery(
    provider_link: ClientProviderLink,
) -> None:
    # Given two accounted read attempts, but an incomplete independent prefix.
    call_contracts.register_operation(
        replace(
            call_contracts.operation_for("SP-DAS"),
            code="SYN-READ",
            operation_class="read",
            billing_class="consulta",
        )
    )
    row = reserve(
        provider_link.tenant_id,
        provider_link.pk,
        replace(request(), operation="SYN-READ"),
    )
    first = claim(provider_link.tenant_id, row.pk)
    assert first is not None
    dispatch = begin_io(first)
    retry = CallResult(CallState.RETRY, 503, "temporary", "sim")
    record_result(dispatch, retry)
    assert finalize(first, retry)
    with tenant_context(provider_link.tenant_id):
        ProviderCall.objects.filter(pk=row.pk).update(next_attempt_at=timezone.now())
    second = claim(provider_link.tenant_id, row.pk)
    assert second is not None
    with ProviderPeer() as peer:
        perform(second, peer)
    restore_before_call()
    for record in provider_journal.read(provider_link.tenant_id):
        if record.attempt_id == first.owner:
            default_storage.delete(record.path)
    # When the later attempt exposes a missing earlier billing receipt.
    with override_settings(PROVIDERS_PAUSED=True), pytest.raises(CommandError):
        call_command("reconcile_provider_operations", restore_ref=str(uuid4()))
    # Then recovery refuses rather than claiming complete per-attempt accounting.
