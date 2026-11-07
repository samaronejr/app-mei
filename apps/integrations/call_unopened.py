"""Proven zero-byte SITFIS attempts retain receipts, not spend or attempt slots."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from os import getpid
from typing import TYPE_CHECKING, Final

from django.utils import timezone

from apps.integrations.budget_models import ProviderReservation
from apps.integrations.budget_settlement import release_unsent
from apps.integrations.call_contracts import CallResult
from apps.integrations.call_models import CallState, ProviderCall, ProviderCallAttempt
from apps.integrations.journal_schema import JournalError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.db.models import QuerySet

    from apps.integrations.journal_schema import FirmJournalRecord, JournalRecord

UNOPENED_RESULT: Final = CallResult(
    CallState.FAILED, None, "wire_veto", "nao", settled_microbrl=0
)


_UNOPENED_WRITES: ContextVar[tuple[int, dict[int, JournalRecord]] | None] = ContextVar(
    "unopened_journal_write", default=None
)


@contextmanager
def _issue_unopened_write(record: JournalRecord) -> Iterator[None]:
    """Seal one append after the dispatch boundary consumed its admission right."""
    grants = {id(record): record}
    token = _UNOPENED_WRITES.set((getpid(), grants))
    try:
        yield
    finally:
        grants.clear()
        _UNOPENED_WRITES.reset(token)


def consume_unopened_write(record: JournalRecord | FirmJournalRecord) -> None:
    """Reject direct, duplicate or copied marker appends at the public writer."""
    if not is_unopened(record.operation, record.result):
        return
    pending = _UNOPENED_WRITES.get()
    if (
        pending is None
        or pending[0] != getpid()
        or pending[1].pop(id(record), None) is not record
    ):
        raise JournalError


def is_unopened(operation: str, result: CallResult | None) -> bool:
    """Recognize only the exact local zero-byte closure for the paid operation."""
    return operation == "SP-SIT-R" and result == UNOPENED_RESULT


def unopened_attempts(call: ProviderCall) -> QuerySet[ProviderCallAttempt]:
    """Return the immutable no-wire closures shared by replay and metering."""
    return ProviderCallAttempt.objects.filter(
        call=call,
        observation="result",
        outcome=UNOPENED_RESULT.state,
        http_status__isnull=True,
        provider_code=UNOPENED_RESULT.provider_code,
        billed="nao",
        response_digest="",
        settled_microbrl=0,
    )


def prior_unopened(call: ProviderCall) -> bool:
    """Only the latest dispatch owner's closed no-wire receipt reopens a slot."""
    if call.operation != "SP-SIT-R" or call.x_request_tag is None:
        return False
    return (
        unopened_attempts(call)
        .filter(attempt_id=call.x_request_tag, attempt_number=call.attempts + 1)
        .exists()
    )


def rewind_unopened(call: ProviderCall, record: JournalRecord) -> bool:
    """Undo only this still-current start; a newer reservation is untouchable.

    The immutable start/result observations keep the abandoned dispatch's audit
    trail. Its released mutable reservation is removed so the same logical slot
    can reserve again, including under a later month's tariff. The effect barrier
    stays immutable and needs this exact owner's proof before another dispatch.
    """
    if not is_unopened(record.operation, record.result):
        raise JournalError
    if (
        call.fence != record.fence
        or call.lease_owner != record.attempt_id
        or call.x_request_tag != record.attempt_id
        or call.attempts != record.attempt_number
        or call.state not in {CallState.STARTED, CallState.UNCERTAIN}
    ):
        return False
    release_unsent(record)
    held = ProviderReservation.objects.select_for_update().get(
        call=call, attempt_number=record.attempt_number
    )
    if (
        held.state != "released"
        or held.microbrl != record.reserved_microbrl
        or held.tariff_revision != record.tariff_revision
    ):
        raise JournalError
    held.delete()
    call.attempts = record.attempt_number - 1
    call.state = CallState.RETRY
    call.reserved_microbrl = 0
    call.dispatch_started_at = None
    call.finalized_at = None
    call.lease_owner = None
    call.lease_until = None
    call.http_status = None
    call.provider_code = UNOPENED_RESULT.provider_code
    call.billed = "nao"
    call.response_digest = ""
    call.settled_microbrl = 0
    call.next_attempt_at = timezone.now()
    call.version += 1
    call.save()
    return True
