"""Explore dispatch, admission and first-byte gaps with production domain code.

The database, clock and session mutex have bounded memory-backed replacements.
The production final guard and both durable writer entry points run unchanged.
Real PostgreSQL tests cover the dispatch capability and financial rewind.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager, nullcontext
from datetime import timedelta
from itertools import pairwise, product
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.cycles.models import Exception as CycleException
from apps.cycles.models import ServiceCycle
from apps.integrations.call_contracts import Claim
from apps.integrations.call_dispatch import Dispatch
from apps.integrations.call_models import CallState
from apps.integrations.journal_schema import JournalRecord
from apps.integrations.models import IntegrationIssue
from apps.integrations.serpro import registration
from apps.integrations.serpro import sitfis as mod
from apps.integrations.serpro.models import FirmIntegrationIssue
from apps.obligations.models import AcquisitionOrigin, Document
from tests.integrations.test_sitfis_interleavings import (
    Rows,
    Scenario,
    _row,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager

    import pytest


class MutexBlockedError(Exception):
    """A writer's transition waits until the modeled wire scope closes."""


class WireScenario(Scenario):
    """Keep admission open across a separately scheduled physical first byte."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, wait_s: int, *, paid: bool
    ) -> None:
        super().__init__(monkeypatch, wait_s, paid=paid)
        self.held = False
        self.pending_writer = False
        self.dispatched = False
        self.guard: AbstractContextManager[None] | None = None
        self.documents: list[Document] = []
        monkeypatch.setattr(mod, "report_mutex", self.mutex)
        monkeypatch.setattr(mod, "tenant_context", lambda *args: nullcontext())
        monkeypatch.setattr(mod, "current_epoch", lambda: 1)
        monkeypatch.setattr(mod, "_locked", lambda report_id: self.report)
        monkeypatch.setattr(
            mod, "_client_of", lambda tenant_id, report_id: self.report.client_id
        )
        monkeypatch.setattr(mod, "abort_unopened", self.abort)
        monkeypatch.setattr(mod, "_ingest_document", self.persist)
        monkeypatch.setattr(Document, "objects", Rows(self.documents))

    @contextmanager
    def mutex(self, *args: object) -> Iterator[None]:
        if self.held:
            raise MutexBlockedError
        self.held = True
        try:
            yield
        finally:
            self.held = False

    def persist(self, **kwargs: object) -> Document:
        self.durable_document = uuid4()
        document = Document(
            pk=self.durable_document,
            tenant_id=self.report.tenant_id,
            client_id=self.report.client_id,
            acquisition_origin=AcquisitionOrigin.FEDERAL_PROVIDER,
            acquisition_receipt_ref=str(self.a.pk),
            portal_visible=False,
            created_at=self.current,
        )
        self.documents.append(document)
        return document

    def receive_recorded(self) -> None:
        try:
            if self.paid:
                document = mod.ingest_system_document(
                    tenant_id=self.report.tenant_id,
                    report_id=self.report.pk,
                    call_id=self.a.pk,
                    reply=self.receipt,
                    payload=b"%PDF-1.7 synthetic model\n%%EOF\n",
                )
                self.receipt.document_id = document.pk
            else:
                mod._receive(
                    self.report.tenant_id, self.report.pk, self.a.pk, self.receipt
                )
                assert self.receipt.received_at is not None
                self.announcements.append((self.receipt.received_at, self.wait_s))
        except MutexBlockedError:
            self.pending_writer = True
            return
        self.pending_writer = False

    def b_dispatch(self) -> None:
        if not self.b_claimed:
            return
        try:
            assert registration.client_authority(self.b)
        except mod.EmissionDeferredError:
            return
        self.b.attempts = 1
        self.b.state = CallState.STARTED
        self.b.dispatch_started_at = self.current
        self.b.fence = 1
        self.b.execution_epoch = 1
        self.b.lease_owner = uuid4()
        self.b.x_request_tag = self.b.lease_owner
        self.b.lease_until = self.current + timedelta(seconds=120)
        self.dispatched = True

    def abort(self, dispatch: Dispatch) -> None:
        assert dispatch.claim.call_id == self.b.pk
        self.b.attempts = 0
        self.b.state = CallState.RETRY
        self.b.dispatch_started_at = None
        self.b.lease_owner = None
        self.dispatched = False

    def b_admission(self) -> None:
        if not self.dispatched:
            return
        assert self.b.lease_owner is not None
        ticket = Claim(
            self.report.tenant_id,
            self.report.client_id,
            self.b.pk,
            self.b.fence,
            self.b.lease_owner,
            self.b.execution_epoch,
        )
        dispatch = Dispatch(
            ticket, JournalRecord.started(self.b, ticket), journaled=True
        )
        self.guard = mod.wire_guard(dispatch)
        try:
            self.guard.__enter__()
        except mod.EmissionDeferredError:
            self.guard = None

    def b_send(self) -> None:
        if self.guard is None:
            return
        try:
            for received_at, wait_s in self.announcements:
                assert self.current >= received_at + timedelta(seconds=wait_s)
            assert self.durable_document is None
            self.sends += 1
        finally:
            self.guard.__exit__(None, None, None)
            self.guard = None
        if self.pending_writer:
            self.receive_recorded()


def _orders(
    remaining: tuple[str, ...],
    edges: tuple[tuple[str, str], ...],
    done: tuple[str, ...] = (),
) -> Iterator[tuple[str, ...]]:
    if not remaining:
        yield done
    for step in remaining:
        if all(before in done for before, after in edges if after == step):
            yield from _orders(
                tuple(other for other in remaining if other != step),
                edges,
                (*done, step),
            )


def test_all_dispatch_admission_and_first_byte_gap_orderings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = (
        "a_claim",
        "a_send",
        "reply_received",
        "receive_recorded",
        "lease_expiry",
        "apply_quarantine",
        "b_claim",
        "b_dispatch",
        "b_admission",
        "b_send",
    )
    chain = (
        "a_claim",
        "a_send",
        "lease_expiry",
        "apply_quarantine",
        "b_claim",
        "b_dispatch",
        "b_admission",
        "b_send",
    )
    edges = (
        *tuple(pairwise(chain)),
        ("a_send", "reply_received"),
        ("reply_received", "receive_recorded"),
    )
    orders = tuple(_orders(steps, edges))
    assert len(orders) == 28
    monkeypatch.setattr(registration, "termo_in_force", lambda *args: True)
    monkeypatch.setattr(
        registration, "authority_for", lambda *args, **kwargs: _row(usable=True)
    )
    for model in (
        FirmIntegrationIssue,
        CycleException,
        IntegrationIssue,
        ServiceCycle,
    ):
        monkeypatch.setattr(model, "objects", Rows([]))
    monkeypatch.setattr(mod, "release", lambda call: None)
    checked = 0
    for order, wait_s, paid in product(orders, (0, 60, 300), (False, True)):
        scenario = WireScenario(monkeypatch, wait_s, paid=paid)
        for step in order:
            getattr(scenario, step)()
        scenario.check()
        assert not scenario.held
        assert not scenario.pending_writer
        checked += 1
    assert checked == 168
    sys.stdout.write(f"SITFIS_DISPATCH_WIRE_INTERLEAVINGS_CHECKED={checked}\n")
