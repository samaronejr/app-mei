"""Bounded two-runner exploration using the production receipt and admission code.

Only persistence and the clock are replaced. The wait fold, current-attempt
projection, fenced policy hook, apply, PDF adoption and final-state checks are
the shipped functions. Database rollback and reservation behavior have separate
real-PostgreSQL tests in test_sitfis_admission.py.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from itertools import permutations, product
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import uuid4

from django.utils import timezone

from apps.clients.models import ClientCompany
from apps.cycles.models import Exception as CycleException
from apps.cycles.models import ServiceCycle
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.models import ClientProviderLink, IntegrationIssue
from apps.integrations.serpro import registration
from apps.integrations.serpro import sitfis as mod
from apps.integrations.serpro.models import FirmIntegrationIssue
from apps.obligations.models import Document, FederalConsultation, FiscalSituationReport

if TYPE_CHECKING:
    from uuid import UUID

    import pytest


class Values(list[object]):
    """The two values-list operations used by the production receipt fold."""

    def first(self) -> object | None:
        return self[0] if self else None


class Rows:
    """A bounded in-memory table; queries preserve the production predicates."""

    def __init__(self, rows: Sequence[object]) -> None:
        self.rows = rows

    def select_for_update(self, **kwargs: object) -> Rows:
        return self

    def filter(self, **kwargs: object) -> Rows:
        return Rows([row for row in self.rows if self._matches(row, kwargs)])

    def exclude(self, **kwargs: object) -> Rows:
        return Rows([row for row in self.rows if not self._matches(row, kwargs)])

    @staticmethod
    def _matches(row: object, predicates: dict[str, object]) -> bool:
        for lookup, expected in predicates.items():
            name, _, operation = lookup.partition("__")
            value = getattr(row, name)
            if operation == "in":
                assert isinstance(expected, Iterable)
                if value not in expected:
                    return False
            elif operation == "gt":
                if value <= expected:
                    return False
            elif operation == "isnull":
                if (value is None) != expected:
                    return False
            elif value != expected:
                return False
        return True

    def order_by(self, *fields: str) -> Rows:
        return self

    def values_list(self, field: str, *, flat: bool) -> Values:
        assert flat
        return Values(getattr(row, field) for row in self.rows)

    def first(self) -> object | None:
        return self.rows[0] if self.rows else None

    def exists(self) -> bool:
        return bool(self.rows)

    def get(self, **kwargs: object) -> object:
        [row] = self.filter(**kwargs).rows
        return row

    def __iter__(self) -> Iterator[object]:
        return iter(self.rows)


def _row(**fields: object) -> SimpleNamespace:
    return SimpleNamespace(**fields, save=lambda **kwargs: None)


class Scenario:
    """Replay one schedule with the real domain code and two memory-backed rows."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, wait_s: int, *, paid: bool
    ) -> None:
        self.current = datetime(2026, 11, 4, 12, tzinfo=UTC)
        self.wait_s, self.paid = wait_s, paid
        monkeypatch.setattr(timezone, "now", self.now)
        self.report = FiscalSituationReport(
            pk=uuid4(),
            tenant_id=uuid4(),
            client_id=uuid4(),
            consultation_id=uuid4(),
            state="aguardando",
            emission_attempts=0,
            protocol="synthetic",
            protocol_date=self.current,
            wait_until=self.current,
            reply_call="",
            reply_wait_ms=None,
            call_replies={},
            document_id=None,
            emitted_at=None,
            version=1,
            updated_at=self.current,
        )
        monkeypatch.setattr(self.report, "save", lambda **kwargs: None)
        self.calls: list[ProviderCall] = []
        consultation = _row(
            pk=self.report.consultation_id,
            outcome="incerto",
            consulted_at=self.current,
            provider_ref="",
            version=1,
        )
        monkeypatch.setattr(FiscalSituationReport, "objects", Rows([self.report]))
        monkeypatch.setattr(ProviderCall, "objects", Rows(self.calls))
        monkeypatch.setattr(Document, "objects", Rows([]))
        monkeypatch.setattr(FederalConsultation, "objects", Rows([consultation]))
        self.a, self.b = self.call(1), self.call(2)
        for call in (self.a, self.b):
            monkeypatch.setattr(call, "save", lambda **kwargs: None)
        self.a.lease_until = self.current + timedelta(seconds=120)
        self.receipt = mod.SitfisReply()
        self.announcements: list[tuple[datetime, int]] = []
        self.durable_document: UUID | None = None
        self.b_claimed = False
        self.sends = 0

    def now(self) -> datetime:
        return self.current

    def call(self, attempt: int) -> ProviderCall:
        return ProviderCall(
            pk=uuid4(),
            tenant_id=self.report.tenant_id,
            client_id=self.report.client_id,
            request_key=mod.emission_request_key(self.report.pk, attempt),
            operation="SP-SIT-R",
            attempts=0,
            state=CallState.RESERVED,
            version=1,
            dispatch_started_at=None,
            finalized_at=None,
            link=ClientProviderLink(
                connection_id=uuid4(),
                tenant_id=self.report.tenant_id,
                client_id=self.report.client_id,
            ),
            client=ClientCompany(
                pk=self.report.client_id, tenant_id=self.report.tenant_id
            ),
        )

    def a_claim(self) -> None:
        self.calls.append(self.a)
        assert registration.client_authority(self.a)
        self.a.state = CallState.CLAIMED

    def a_send(self) -> None:
        self.send(self.a)

    def send(self, call: ProviderCall) -> None:
        assert registration.client_authority(call)
        # This oracle is independent of the production maximum fold. A reply
        # exists to other runners when recorded; the accepted default still
        # applies to a sent call with no recorded reply.
        for received_at, wait_s in self.announcements:
            assert self.current >= received_at + timedelta(seconds=wait_s)
        assert self.durable_document is None
        call.attempts = 1
        call.state = CallState.STARTED
        call.dispatch_started_at = self.current
        self.sends += 1

    def reply_received(self) -> None:
        self.receipt.received_at = self.current
        self.receipt.wait = None if self.paid else timedelta(seconds=self.wait_s)

    def receive_recorded(self) -> None:
        if self.paid:
            self.receipt.document_id = uuid4()
            self.durable_document = self.receipt.document_id
        else:
            assert self.receipt.received_at is not None
            self.announcements.append((self.receipt.received_at, self.wait_s))
        mod._store_reply(self.report, self.a.pk, self.receipt)
        if self.paid:
            mod._received_pdf(self.report)

    def lease_expiry(self) -> None:
        assert self.a.lease_until is not None
        self.current = max(self.current, self.a.lease_until + timedelta(seconds=1))

    def apply_quarantine(self) -> None:
        self.a.state = CallState.UNCERTAIN
        if self.report.state not in mod.FINAL_STATES:
            mod._apply(self.report, self.a, "SP-SIT-R", 1)

    def b_claim(self) -> None:
        assert self.report.wait_until is not None
        self.current = max(self.current, self.report.wait_until)
        if self.report.document_id is not None or self.report.state in mod.FINAL_STATES:
            return
        self.calls.append(self.b)
        try:
            assert registration.client_authority(self.b)
        except mod.EmissionDeferredError:
            return
        self.b.state = CallState.CLAIMED
        self.b_claimed = True

    def b_send(self) -> None:
        if self.b_claimed:
            try:
                self.send(self.b)
            except mod.EmissionDeferredError:
                return

    def check(self) -> None:
        if self.paid:
            mod._received_pdf(self.report)
            assert self.report.state == "emitido"
            assert self.report.document_id == self.durable_document
        assert 1 <= self.sends <= 2


def test_every_bounded_two_runner_ordering_preserves_wait_and_paid_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explore all linear extensions, three waits and both reply kinds."""
    steps = (
        "a_claim",
        "a_send",
        "reply_received",
        "receive_recorded",
        "lease_expiry",
        "apply_quarantine",
        "b_claim",
        "b_send",
    )
    edges = (
        ("a_claim", "a_send"),
        ("a_send", "reply_received"),
        ("reply_received", "receive_recorded"),
        ("a_send", "lease_expiry"),
        ("lease_expiry", "apply_quarantine"),
        ("apply_quarantine", "b_claim"),
        ("b_claim", "b_send"),
    )
    orderings = [
        order
        for order in permutations(steps)
        if all(order.index(a) < order.index(b) for a, b in edges)
    ]
    assert len(orderings) == 15
    monkeypatch.setattr(registration, "termo_in_force", lambda *args: True)
    monkeypatch.setattr(
        registration, "authority_for", lambda *args, **kwargs: _row(usable=True)
    )
    monkeypatch.setattr(FirmIntegrationIssue, "objects", Rows([]))
    monkeypatch.setattr(CycleException, "objects", Rows([]))
    monkeypatch.setattr(IntegrationIssue, "objects", Rows([]))
    monkeypatch.setattr(ServiceCycle, "objects", Rows([]))
    monkeypatch.setattr(mod, "release", lambda call: None)
    checked = 0
    for order, wait_s, paid in product(orderings, (0, 60, 300), (False, True)):
        scenario = Scenario(monkeypatch, wait_s, paid=paid)
        for step in order:
            getattr(scenario, step)()
        scenario.check()
        checked += 1
    assert checked == 90
    print(f"SITFIS_INTERLEAVINGS_CHECKED={checked}")  # noqa: T201


def test_applying_a_newer_short_reply_keeps_the_older_announced_task_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task must sleep until the same maximum that its send fence enforces."""
    monkeypatch.setattr(registration, "termo_in_force", lambda *args: True)
    monkeypatch.setattr(
        registration, "authority_for", lambda *args, **kwargs: _row(usable=True)
    )
    monkeypatch.setattr(FirmIntegrationIssue, "objects", Rows([]))
    monkeypatch.setattr(CycleException, "objects", Rows([]))
    monkeypatch.setattr(IntegrationIssue, "objects", Rows([]))
    monkeypatch.setattr(ServiceCycle, "objects", Rows([]))
    scenario = Scenario(monkeypatch, 300, paid=False)
    for step in (
        "a_claim",
        "a_send",
        "reply_received",
        "lease_expiry",
        "apply_quarantine",
        "b_claim",
        "b_send",
        "receive_recorded",
    ):
        getattr(scenario, step)()
    scenario.b.state = CallState.WAITING
    short_reply = mod.SitfisReply(
        received_at=scenario.current, wait=timedelta(seconds=0)
    )
    mod._store_reply(scenario.report, scenario.b.pk, short_reply)
    mod._apply(scenario.report, scenario.b, "SP-SIT-R", 2)
    assert scenario.receipt.received_at is not None
    expected = scenario.receipt.received_at + timedelta(seconds=300)
    assert not mod._emission_due(scenario.report)
    assert mod.next_run_at(scenario.report) == expected
