"""Until the builder lands, no fiscal Ciencia proposal can be created, decided or run.

AXRM-022-SCAFFOLD: rewritten in 22-06 (no fiscal effect registered), deleted in 22-14.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, cast

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.cycles.effects.executors import execute
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.models import (
    ActionExecution,
    ActionProposal,
    Approval,
    CycleEvent,
    ProposalState,
    ServiceCycle,
)
from apps.cycles.services.approvals import approve, propose, revalidate
from apps.cycles.services.proposed_transitions import propose_waiver
from tests.integrations.ciencia_fixtures import manifestation_kind
from tests.integrations.ciencia_storage_rows import (
    StorageWorld,
    fiscal_proposal,
    storage_world,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from apps.obligations.models import FiscalDocument
    from tests.integrations.capture_fixtures import CaptureWorld

pytestmark = pytest.mark.django_db(transaction=True)
REFUSAL = "fiscal_manifestation_unavailable"
WRITES = ("INSERT", "UPDATE", "DELETE")


@dataclass
class Closed:
    """A proposal-ready graph: open cycle, assigned initiator, one received NF-e."""

    storage: StorageWorld

    @property
    def world(self) -> CaptureWorld:
        return self.storage.capture

    @property
    def document(self) -> FiscalDocument:
        return self.storage.document

    @property
    def cycle(self) -> ServiceCycle:
        with tenant_context(self.world.tenant_id):
            return ServiceCycle.objects.get(pk=self.world.setup.cycle.pk)

    def inputs(self, kind: object, *, documents: int = 1) -> ProposalInput:
        return ProposalInput(
            action_kind=cast("ActionKind", kind),
            step_key="ciencia",
            fiscal_document_ids=(self.document.pk,) * documents,
            provider_link_id=self.world.link.pk if documents else None,
        )

    def stored(self, state: str) -> ActionProposal:
        return fiscal_proposal(self.storage, state)

    def counts(self) -> tuple[list[UUID], int, int, int, int]:
        with tenant_context(self.world.tenant_id):
            return (
                sorted(ActionProposal.objects.values_list("pk", flat=True)),
                CycleEvent.objects.count(),
                Approval.objects.count(),
                ActionExecution.objects.count(),
                self.cycle.version,
            )


@pytest.fixture
def closed(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Closed:
    return Closed(storage_world(settings, tmp_path, monkeypatch))


def _attempt(closed: Closed, call: Callable[[], object]) -> tuple[object, list[str]]:
    """Run the real command; capture its outcome and every statement it issued."""
    outcome: object
    with (
        tenant_context(closed.world.tenant_id),
        CaptureQueriesContext(connection) as queries,
    ):
        try:
            outcome = call()
        except Exception as error:  # noqa: BLE001 - the outcome is the assertion
            outcome = error
    return outcome, [query["sql"] for query in queries.captured_queries]


def _refused_without_writes(outcome: object, statements: list[str]) -> None:
    assert isinstance(outcome, ValidationError), outcome
    assert outcome.code == REFUSAL, outcome
    written = [sql for sql in statements if sql.lstrip().upper().startswith(WRITES)]
    assert written == [], written


@pytest.mark.parametrize(
    ("kind", "documents"),
    [
        (ActionKind.FISCAL_MANIFESTATION, 1),
        (ActionKind.FISCAL_MANIFESTATION, 2),
        ("fiscal_manifestation", 0),
    ],
    ids=["one-document", "same-document-twice", "bare-string-no-selection"],
)
def test_propose_refuses_every_fiscal_selection_without_writing(
    closed: Closed, kind: object, documents: int
) -> None:
    before = closed.counts()
    cycle = closed.cycle
    outcome, statements = _attempt(
        closed,
        lambda: propose(
            closed.world.setup.graph.staff,
            cycle.pk,
            ProposalDraft(
                closed.inputs(kind, documents=documents),
                expires_at=timezone.now() + timedelta(hours=72),
            ),
            expected_version=cycle.version,
        ),
    )
    _refused_without_writes(outcome, statements)
    assert closed.counts() == before


def test_propose_refuses_a_revision_of_a_stored_fiscal_row(closed: Closed) -> None:
    prior = closed.stored(ProposalState.AWAITING)
    before = closed.counts()
    cycle = closed.cycle
    outcome, statements = _attempt(
        closed,
        lambda: propose(
            closed.world.setup.graph.staff,
            cycle.pk,
            ProposalDraft(
                closed.inputs(manifestation_kind()),
                expires_at=timezone.now() + timedelta(hours=72),
                previous_id=prior.pk,
                previous_version=prior.version,
            ),
            expected_version=cycle.version,
        ),
    )
    _refused_without_writes(outcome, statements)
    assert closed.counts() == before
    with tenant_context(closed.world.tenant_id):
        kept = ActionProposal.objects.get(pk=prior.pk)
    assert (kept.state, kept.version, bytes(kept.manifest_bytes)) == (
        prior.state,
        prior.version,
        bytes(prior.manifest_bytes),
    )


def test_a_legacy_proposal_on_the_same_graph_still_succeeds(closed: Closed) -> None:
    request = closed.world.setup.request
    with tenant_context(closed.world.tenant_id):
        proposal = propose_waiver(
            closed.world.setup.graph.staff,
            request.pk,
            reason="Synthetic reason",
            expected_version=request.version,
        )
    assert proposal.action_kind == ActionKind.REQUEST_WAIVER
    assert proposal.state == ProposalState.AWAITING


@pytest.mark.parametrize("command", ["approve", "revalidate", "execute"])
def test_a_stored_fiscal_row_cannot_be_decided_or_executed(
    closed: Closed, command: str
) -> None:
    state = ProposalState.APPROVED if command == "execute" else ProposalState.AWAITING
    row = closed.stored(state)
    graph = closed.world.setup.graph
    before = closed.counts()
    calls: dict[str, Callable[[], object]] = {
        "approve": lambda: approve(graph.actor, row.pk, expected_version=row.version),
        "revalidate": lambda: revalidate(graph.actor, row.pk),
        "execute": lambda: execute(graph.staff, row.pk, expected_version=row.version),
    }
    outcome, statements = _attempt(closed, calls[command])
    # Refused as a validation error before any decision, execution marker or event.
    assert isinstance(outcome, ValidationError), outcome
    assert closed.counts() == before
    written = [sql for sql in statements if sql.lstrip().upper().startswith(WRITES)]
    assert written == [], written
