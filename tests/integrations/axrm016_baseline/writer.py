"""Frozen ordinary result writer from protocol revision 0e79391."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from apps.core.tasks import assert_no_transaction
from apps.integrations.budget_settlement import billing_result
from tests.integrations.axrm016_baseline import journal as provider_journal

if TYPE_CHECKING:
    from apps.integrations.call_contracts import CallResult
    from apps.integrations.call_dispatch import Dispatch


def record_result(dispatch: Dispatch, result: CallResult) -> None:
    """Durably record the reply before the fenced database finalization."""
    assert_no_transaction()
    if dispatch.journaled:
        provider_journal.append(
            replace(dispatch.receipt, result=billing_result(dispatch.receipt, result))
        )
