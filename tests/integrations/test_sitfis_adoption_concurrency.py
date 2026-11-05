"""Paid-PDF adoption and a competing admission cannot form a lock cycle."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from django.db import connections

from apps.integrations import call_finalization, call_protocol
from tests.fakes.provider import Reply
from tests.integrations.sitfis_support import (
    World,
    clock,
    report_reply,
    sitfis,
    world,
)
from tests.integrations.test_sitfis_admission import _ready

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import Future

    from apps.integrations.call_models import ProviderCall

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "world"]


def _pause[**P, T](
    callback: Callable[P, T],
    role: str,
    ready: threading.Event,
    proceed: threading.Event,
) -> Callable[P, T]:
    def held(*args: P.args, **kwargs: P.kwargs) -> T:
        if threading.current_thread().name == role:
            ready.set()
            assert proceed.wait(timeout=20)
        return callback(*args, **kwargs)

    return held


def _worker(role: str, action: Callable[[], object]) -> object:
    threading.current_thread().name = role
    try:
        return action()
    finally:
        connections.close_all()


def test_paid_pdf_adoption_and_competing_admission_do_not_deadlock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = sitfis()
    report, first = _ready(world)
    world.script(Reply(body=report_reply().body, held=True))
    world.gateway.received.clear()
    document_ready, finish_original = threading.Event(), threading.Event()
    adoption_locked, finish_adoption = threading.Event(), threading.Event()
    admission_at_authority = threading.Event()
    real_admit = mod.admit_emission

    def observe_admission(call: ProviderCall) -> None:
        if threading.current_thread().name == "competing-admission":
            admission_at_authority.set()
        real_admit(call)

    monkeypatch.setattr(
        call_protocol,
        "finalize",
        _pause(
            call_finalization.finalize,
            "paid-original",
            document_ready,
            finish_original,
        ),
    )
    monkeypatch.setattr(
        mod,
        "_adopt",
        _pause(mod._adopt, "pdf-adoption", adoption_locked, finish_adoption),
    )
    monkeypatch.setattr(mod, "admit_emission", observe_admission)
    with ThreadPoolExecutor(max_workers=3) as pool:
        original = pool.submit(
            _worker,
            "paid-original",
            lambda: mod._run(world.tenant_id, report.pk, first),
        )
        adoption: Future[object] | None = None
        competing: Future[object] | None = None
        try:
            assert world.gateway.received.wait(timeout=15)
            world.clock.advance(seconds=121)
            world.advance(report)
            world.clock.advance(seconds=60)
            second = mod._decide(world.tenant_id, report.pk)
            assert second is not None
            with monkeypatch.context() as reserve_only:
                reserve_only.setattr(mod, "claim", lambda *args: None)
                mod._run(world.tenant_id, report.pk, second)
            world.gateway.release.set()
            assert document_ready.wait(timeout=15)
            assert world.reload(report).document_id is not None
            adoption = pool.submit(
                _worker, "pdf-adoption", lambda: world.advance(report)
            )
            assert adoption_locked.wait(timeout=15)
            competing = pool.submit(
                _worker,
                "competing-admission",
                lambda: mod._run(world.tenant_id, report.pk, second),
            )
            assert admission_at_authority.wait(timeout=15)
            finish_adoption.set()
            adoption.result(timeout=15)
            competing.result(timeout=15)
        finally:
            world.gateway.release.set()
            finish_adoption.set()
            finish_original.set()
            for future in (adoption, competing, original):
                if future is not None:
                    future.result(timeout=25)
    assert world.reload(report).state == "emitido"
    assert len(world.sent("/Emitir")) == 1
    assert world.budget().reserved == 0
