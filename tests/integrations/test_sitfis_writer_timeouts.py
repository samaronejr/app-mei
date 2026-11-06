"""A real coordination timeout retries the same durable input without another send."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
from django.db import connections

from apps.integrations.serpro import sitfis_wire
from apps.integrations.serpro.sitfis_wire import ReportWireTimeoutError
from tests.integrations.sitfis_support import (
    World,
    clock,
    processing_202,
    protocol_reply,
    report_reply,
    sitfis,
    world,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from datetime import datetime
    from uuid import UUID

    from apps.obligations.models import FiscalSituationReport

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "world"]


def _assert_input_kept(
    report: FiscalSituationReport, received_at: datetime, writer: str
) -> None:
    if writer == "receipt":
        assert report.state == "aguardando"
        assert any(
            receipt["received_at"] == received_at.isoformat()
            and receipt["wait_ms"] == 300_000
            for values in report.call_replies.values()
            for receipt in values
        )
    else:
        assert report.state == "emitido"
        assert report.document_id is not None


def _contend[**P, T](
    callback: Callable[P, T],
    hold_report: Callable[[], None],
    owned: threading.Event,
    timed_out: threading.Event,
) -> Callable[P, T]:
    def busy(*args: P.args, **kwargs: P.kwargs) -> T:
        holder = threading.Thread(target=hold_report)
        holder.start()
        try:
            assert owned.wait(timeout=10)
            return callback(*args, **kwargs)
        finally:
            timed_out.set()
            holder.join(timeout=10)
            assert not holder.is_alive()

    return busy


@pytest.mark.parametrize("writer", ["receipt", "pdf", "adoption"])
def test_acquisition_timeout_preserves_and_retries_the_same_input(
    world: World, monkeypatch: pytest.MonkeyPatch, writer: str
) -> None:
    mod = sitfis()
    world.script(protocol_reply(30_000))
    report = world.advance(world.request())
    world.clock.advance(seconds=30)
    received_at = world.clock.now()
    world.script(processing_202(300_000) if writer == "receipt" else report_reply())
    real_mutex = sitfis_wire.report_mutex
    owned, timed_out = threading.Event(), threading.Event()
    attempts: list[str] = []

    def hold_report() -> None:
        try:
            with real_mutex(world.tenant_id, report.pk):
                owned.set()
                assert timed_out.wait(timeout=10)
        finally:
            connections.close_all()

    @contextmanager
    def short_wait(tenant_id: UUID, report_id: UUID) -> Iterator[None]:
        attempts.append("acquire")
        try:
            with real_mutex(tenant_id, report_id, timeout_ms=10):
                yield
        except ReportWireTimeoutError:
            attempts.append("timeout")
            timed_out.set()
            raise

    target = {
        "receipt": "_receive",
        "pdf": "ingest_system_document",
        "adoption": "_deferred",
    }[writer]
    monkeypatch.setattr(sitfis_wire, "report_mutex", short_wait)
    monkeypatch.setattr(
        mod, target, _contend(getattr(mod, target), hold_report, owned, timed_out)
    )
    try:
        final = world.advance(report)
        if writer == "adoption":
            [call] = world.calls("SP-SIT-R")
            mod._deferred(world.tenant_id, report.pk, mod.EmissionDeferredError(call))
            final = world.reload(report)
    finally:
        assert attempts.count("acquire") > attempts.count("timeout")
    assert "timeout" in attempts
    assert attempts.count("acquire") > attempts.count("timeout")
    assert len(world.sent("/Emitir")) == 1
    _assert_input_kept(final, received_at, writer)
    assert world.budget().reserved == 0
