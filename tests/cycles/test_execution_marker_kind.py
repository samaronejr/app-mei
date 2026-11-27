"""An execution marker is exact, absent or conflicting; only an exact one is proof.

Added over 8f63bbf (AXRM-022 part 4, kept). The exact foreign key binds a marker to its
proposal, revision and digest but not to its kind, cycle or business intent, so every
reader compares all eight fields: tenant, client, cycle, proposal, revision, digest,
intent and kind. A conflicting marker is neither proof of execution nor permission to
run the effect again.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import TYPE_CHECKING

import pytest
from django.core import mail
from django.db import connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.authz.models import Role
from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.cycles.completion_checks import completion_items
from apps.cycles.display_completion import display_completion_items
from apps.cycles.display_reads import load_cycle_detail
from apps.cycles.effects.executors import _queue, execute
from apps.cycles.effects.guards import actions_terminal, effects_started
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.errors import IllegalTransition
from apps.cycles.evidence_index import capture_index
from apps.cycles.models import (
    ActionExecution,
    ActionProposal,
    ClientPublication,
    CycleState,
    EvidenceRequest,
    ProposalState,
    PublicationState,
    RequestState,
    ServiceCycle,
)
from apps.cycles.services.approvals import approve, propose, revalidate
from apps.cycles.services.completion import apply_completion, propose_reopen
from apps.cycles.services.cycle import open_cycle
from apps.cycles.services.proposed_transitions import propose_completion
from apps.cycles.services.requests import waive_request
from apps.delivery import gateway
from apps.delivery.errors import IntentConflict
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.services import claim_batch
from apps.tenants.models import Membership
from tests.browser.harness import mfa_session_cookie
from tests.cycles.completion_fixtures import (
    CompletionSetup,
    approved_completion,
    close_cycle,
    completion_setup,
)
from tests.cycles.publication_fixtures import PublicationGraph, publication_graph
from tests.cycles.test_approval_revocation import reviewable
from tests.integrations.ciencia_storage_rows import (
    StorageWorld,
    fiscal_execution,
    legacy_proposal,
    storage_world,
)
from tests.support import enrol_totp

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from tests.cycles.approval_fixtures import ApprovalGraph

pytestmark = pytest.mark.django_db(transaction=True)
WRITES = ("INSERT", "UPDATE", "DELETE")
OTHER_INTENT = hashlib.sha256(b"axrm022-another-intent").hexdigest()


def marker(proposal: ActionProposal, **changes: object) -> ActionExecution:
    """A marker copying the proposal's eight fields, except the ones changed."""
    fields: dict[str, object] = {
        "tenant_id": proposal.tenant_id,
        "client_id": proposal.client_id,
        "cycle_id": proposal.cycle_id,
        "proposal": proposal,
        "proposal_revision": proposal.revision,
        "digest": proposal.digest,
        "business_intent_key": proposal.business_intent_key,
        "action_kind": proposal.action_kind,
        "executed_by_id": proposal.initiator_id,
    }
    fields.update(changes)
    with tenant_context(proposal.tenant_id):
        return ActionExecution.objects.create(**fields)


def wrong_kind(proposal: ActionProposal) -> str:
    return (
        ActionKind.EMAIL_NOTICE
        if proposal.action_kind == ActionKind.REQUEST_WAIVER
        else ActionKind.REQUEST_WAIVER
    )


def _attempt(tenant: UUID, call: Callable[[], object]) -> tuple[object, list[str]]:
    outcome: object
    with tenant_context(tenant), CaptureQueriesContext(connection) as queries:
        try:
            outcome = call()
        except Exception as error:  # noqa: BLE001 - the outcome is the assertion
            outcome = error
    return outcome, [query["sql"] for query in queries.captured_queries]


def _no_writes(statements: list[str]) -> None:
    written = [sql for sql in statements if sql.lstrip().upper().startswith(WRITES)]
    assert written == [], written


def _markers(proposal: ActionProposal) -> list[tuple[object, ...]]:
    with tenant_context(proposal.tenant_id):
        return sorted(
            ActionExecution.objects.filter(proposal=proposal).values_list(
                "pk", "cycle_id", "business_intent_key", "action_kind"
            )
        )


def _messages(proposal: ActionProposal) -> int:
    with tenant_context(proposal.tenant_id):
        return OutboundMessage.objects.filter(proposal_id=proposal.pk).count()


@pytest.fixture
def world(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> StorageWorld:
    return storage_world(settings, tmp_path, monkeypatch)


# Section 2: the executor replays only an exact marker.


def test_a_fiscal_marker_on_a_legacy_waiver_is_a_conflict_not_a_replay(
    world: StorageWorld,
) -> None:
    receipt = fiscal_execution(world)
    with tenant_context(world.tenant_id):
        proposal = ActionProposal.objects.get(pk=receipt.proposal_id)
    before = _markers(proposal)
    outcome, statements = _attempt(
        world.tenant_id,
        lambda: execute(
            world.graph.staff, proposal.pk, expected_version=proposal.version
        ),
    )
    assert isinstance(outcome, IntentConflict), outcome
    _no_writes(statements)
    assert _markers(proposal) == before
    with tenant_context(world.tenant_id):
        request = EvidenceRequest.objects.get(pk=world.capture.setup.request.pk)
    assert request.state != RequestState.WAIVED


def test_a_marker_for_the_proposal_under_another_intent_is_a_conflict() -> None:
    setup = reviewable("marker-other-intent")
    proposal = setup.proposal
    marker(proposal, business_intent_key=OTHER_INTENT)
    before = _markers(proposal)
    outcome, statements = _attempt(
        setup.graph.tenant.pk,
        lambda: execute(
            setup.graph.actor, proposal.pk, expected_version=proposal.version
        ),
    )
    assert isinstance(outcome, IntentConflict), outcome
    _no_writes(statements)
    assert _markers(proposal) == before
    assert _messages(proposal) == 0


def test_a_marker_on_another_cycle_of_the_client_is_a_conflict() -> None:
    setup = reviewable("marker-other-cycle")
    proposal = setup.proposal
    graph = setup.graph
    with tenant_context(graph.tenant.pk):
        other = open_cycle(
            graph.actor,
            graph.engagement.pk,
            date(2026, 10, 1),
            graph.definition.pk,
            idempotency_key="marker-other-cycle-next",
        )
    assert other.id != proposal.cycle_id
    marker(proposal, cycle_id=other.id)
    before = _markers(proposal)
    outcome, statements = _attempt(
        graph.tenant.pk,
        lambda: execute(graph.actor, proposal.pk, expected_version=proposal.version),
    )
    assert isinstance(outcome, IntentConflict), outcome
    _no_writes(statements)
    assert _markers(proposal) == before
    assert _messages(proposal) == 0


def test_an_exact_legacy_marker_is_still_replayed_without_rerunning() -> None:
    setup = reviewable("marker-exact-replay")
    proposal = setup.proposal
    with tenant_context(setup.graph.tenant.pk):
        first = execute(setup.graph.actor, proposal.pk, expected_version=2)
        assert (first.executed, first.replayed) == (True, False)
        again = execute(
            setup.graph.actor, proposal.pk, expected_version=first.proposal.version
        )
    assert (again.executed, again.replayed) == (True, True)
    assert len(_markers(proposal)) == 1
    assert _messages(proposal) == 1


def _session(setup: ApprovalGraph, settings: SettingsWrapper) -> Client:
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    enrol_totp(setup.graph.actor)
    host = f"{setup.graph.tenant.slug}.localhost"
    client = Client(SERVER_NAME=host)
    cookie = mfa_session_cookie(setup.graph.actor, f"http://{host}/")
    client.cookies[cookie["name"]] = cookie["value"]
    return client


def _card_setup(state: str) -> ApprovalGraph:
    return reviewable(f"marker-card-{state}")


@pytest.mark.parametrize("state", ["awaiting", "approved"])
def test_the_card_shows_no_effect_for_a_conflicting_marker(
    settings: SettingsWrapper, state: str
) -> None:
    setup = _card_setup(state)
    proposal = setup.proposal
    if state == "awaiting":
        with tenant_context(setup.graph.tenant.pk):
            proposal = propose(
                setup.graph.actor,
                setup.cycle.pk,
                ProposalDraft(
                    ProposalInput.from_data(proposal.input_spec),
                    proposal.expires_at,
                    previous_id=proposal.pk,
                    previous_version=proposal.version,
                ),
                expected_version=ServiceCycle.objects.get(pk=setup.cycle.pk).version,
            )
        assert proposal.state == ProposalState.AWAITING
    marker(proposal, action_kind=wrong_kind(proposal))
    response = _session(setup, settings).get(
        reverse("approval-card", args=[proposal.pk])
    )
    assert response.status_code == 200
    assert response.context["execution"] is None
    assert response.context["may_execute"] is False
    assert 'value="execute"' not in response.content.decode()


def test_the_card_shows_an_exact_marker_as_the_effect(
    settings: SettingsWrapper,
) -> None:
    setup = reviewable("marker-card-exact")
    proposal = setup.proposal
    receipt = marker(proposal)
    response = _session(setup, settings).get(
        reverse("approval-card", args=[proposal.pk])
    )
    assert response.status_code == 200
    assert response.context["execution"] == receipt
    assert response.context["may_execute"] is False


# Section 5 (reviewer ruling B): every completion and marker reader uses the same match.


def _blockers(setup: ApprovalGraph, view: str) -> set[str]:
    with tenant_context(setup.graph.tenant.pk):
        cycle = ServiceCycle.objects.get(pk=setup.cycle.pk)
        if view == "locked":
            return {item.key for item in completion_items(cycle)}
        reads = load_cycle_detail(setup.graph.actor, cycle.pk)
        return {item.key for item in display_completion_items(reads)}


@pytest.mark.parametrize("view", ["locked", "displayed"])
def test_a_wrong_kind_marker_leaves_the_action_blocker_visible(view: str) -> None:
    setup = reviewable(f"marker-blocker-{view}")
    proposal = setup.proposal
    marker(proposal, action_kind=wrong_kind(proposal))
    assert f"actions:{proposal.pk}:{proposal.version}" in _blockers(setup, view)


@pytest.mark.parametrize("view", ["locked", "displayed"])
def test_an_exact_marker_clears_the_action_blocker(view: str) -> None:
    setup = reviewable(f"marker-cleared-{view}")
    proposal = setup.proposal
    marker(proposal)
    assert not any(
        key.startswith(f"actions:{proposal.pk}:") for key in _blockers(setup, view)
    )


@pytest.mark.parametrize("exact", [False, True], ids=["wrong-kind", "exact"])
def test_only_an_exact_marker_decides_an_action(exact: bool) -> None:
    setup = reviewable(f"marker-terminal-{exact}")
    proposal = setup.proposal
    marker(proposal, **({} if exact else {"action_kind": wrong_kind(proposal)}))
    with tenant_context(setup.graph.tenant.pk):
        decided = actions_terminal(ServiceCycle.objects.get(pk=setup.cycle.pk))
    assert decided is exact


@pytest.mark.parametrize("exact", [False, True], ids=["other-intent", "exact"])
def test_only_an_exact_marker_starts_an_effect(
    world: StorageWorld, exact: bool
) -> None:
    proposal = legacy_proposal(world)
    marker(proposal, **({} if exact else {"business_intent_key": OTHER_INTENT}))
    with tenant_context(world.tenant_id):
        started = effects_started(ServiceCycle.objects.get(pk=proposal.cycle_id))
    assert started is exact


def _waived(world: StorageWorld, *, exact: bool) -> EvidenceRequest:
    """Approve the legacy waiver; run it for real, or waive beside a wrong marker."""
    proposal = legacy_proposal(world)
    graph = world.graph
    with tenant_context(world.tenant_id):
        approved = approve(
            graph.actor, proposal.pk, expected_version=proposal.version
        ).proposal
        if exact:
            assert execute(
                graph.staff, approved.pk, expected_version=approved.version
            ).executed
        else:
            request = EvidenceRequest.objects.get(pk=world.capture.setup.request.pk)
            waive_request(
                graph.staff,
                request.pk,
                reason="Synthetic reason",
                expected_version=request.version,
            )
    if not exact:
        marker(approved, business_intent_key=OTHER_INTENT)
    with tenant_context(world.tenant_id):
        request = EvidenceRequest.objects.get(pk=world.capture.setup.request.pk)
    assert request.state == RequestState.WAIVED
    return request


@pytest.mark.parametrize("view", ["locked", "displayed"])
@pytest.mark.parametrize("exact", [False, True], ids=["other-intent", "exact"])
def test_only_an_exact_receipt_validates_a_waiver(
    world: StorageWorld, view: str, exact: bool
) -> None:
    request = _waived(world, exact=exact)
    with tenant_context(world.tenant_id):
        if view == "locked":
            cycle = ServiceCycle.objects.get(pk=request.cycle_id)
            valid = f"requests:{request.pk}:{request.version}" not in {
                item.key for item in completion_items(cycle)
            }
        else:
            reads = load_cycle_detail(world.graph.actor, request.cycle_id)
            valid = request.pk in reads.valid_waivers
    assert valid is exact


def test_completion_refuses_a_wrong_kind_receipt() -> None:
    setup = completion_setup("marker-completion")
    with tenant_context(setup.graph.tenant.pk):
        proposal = approved_completion(setup)
    marker(proposal, action_kind=ActionKind.REQUEST_WAIVER)
    refused: object = None
    with tenant_context(setup.graph.tenant.pk):
        try:
            with transaction.atomic():
                apply_completion(setup.graph.actor, proposal)
        except IllegalTransition as error:
            refused = error
        cycle = ServiceCycle.objects.get(pk=setup.cycle.pk)
    assert isinstance(refused, IllegalTransition), refused
    assert cycle.state not in {CycleState.COMPLETED, CycleState.WITH_PENDING}


def test_a_reopening_refuses_a_wrong_kind_receipt() -> None:
    """The reopen path returns before the completion checklist, so only the receipt
    check stands between a non-exact marker and the amendment."""
    setup = completion_setup("marker-reopen-receipt")
    graph = setup.graph
    with tenant_context(graph.tenant.pk):
        close_cycle(setup)
        cycle = ServiceCycle.objects.get(pk=setup.cycle.pk)
        reviewer = User.objects.create_user(email="marker-reopen-receipt@example.test")
        Membership.objects.create(
            tenant=graph.tenant, user=reviewer, role=Role.OPERATIONS_ADMIN
        )
        reopening = propose_reopen(
            graph.actor,
            cycle.pk,
            reason="Conferir uma evidência nova.",
            affected_scope=(cycle.pk,),
            expected_version=cycle.version,
        )
        decided = approve(reviewer, reopening.pk, expected_version=reopening.version)
        cycle.refresh_from_db()
    marker(decided.proposal, action_kind=ActionKind.REQUEST_WAIVER)
    refused: object = None
    with tenant_context(graph.tenant.pk):
        try:
            with transaction.atomic():
                apply_completion(reviewer, decided.proposal)
        except IllegalTransition as error:
            refused = error
        after = ServiceCycle.objects.get(pk=cycle.pk)
    assert isinstance(refused, IllegalTransition), refused
    assert (after.state, after.amendment, after.version) == (
        cycle.state,
        cycle.amendment,
        cycle.version,
    )


def test_completion_accepts_its_exact_receipt() -> None:
    setup = completion_setup("marker-completion-exact")
    with tenant_context(setup.graph.tenant.pk):
        close_cycle(setup)
        cycle = ServiceCycle.objects.get(pk=setup.cycle.pk)
    assert cycle.state == CycleState.COMPLETED


def _reopened_with_pending(slug: str, *, exact: bool) -> ActionProposal:
    """Close with one justified pending notice, mark it, then reopen for real."""
    setup: CompletionSetup = completion_setup(slug)
    graph = setup.graph
    with tenant_context(graph.tenant.pk):
        contact = ClientContact.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            name="Synthetic recipient",
            email="recipient@example.test",
        )
        cycle = ServiceCycle.objects.get(pk=setup.cycle.pk)
        pending = propose(
            graph.staff,
            cycle.pk,
            ProposalDraft(
                ProposalInput(
                    action_kind=ActionKind.EMAIL_NOTICE,
                    step_key="notice",
                    recipient_ids=(contact.pk,),
                    subject="Aviso pendente",
                    body="Aviso ainda não aprovado.",
                ),
                timezone.now() + timedelta(days=1),
            ),
            expected_version=cycle.version,
        )
        cycle.refresh_from_db()
        capture_index(graph.actor, cycle.pk, expected_version=cycle.version)
        cycle.refresh_from_db()
        keys = {item.key for item in completion_items(cycle)}
        closing = propose_completion(
            graph.staff,
            cycle.pk,
            to_state=CycleState.WITH_PENDING,
            reason=dict.fromkeys(sorted(keys), "Pendência justificada."),
            expected_version=cycle.version,
        )
        closing = approve(
            graph.actor, closing.pk, expected_version=closing.version
        ).proposal
        assert execute(
            graph.actor, closing.pk, expected_version=closing.version
        ).executed
    marker(pending, **({} if exact else {"action_kind": ActionKind.REQUEST_WAIVER}))
    with tenant_context(graph.tenant.pk):
        cycle = ServiceCycle.objects.get(pk=setup.cycle.pk)
        reviewer = User.objects.create_user(email=f"{slug}-reviewer@example.test")
        Membership.objects.create(
            tenant=graph.tenant, user=reviewer, role=Role.OPERATIONS_ADMIN
        )
        reopening = propose_reopen(
            graph.actor,
            cycle.pk,
            reason="Conferir o aviso pendente.",
            affected_scope=(cycle.pk,),
            expected_version=cycle.version,
        )
        decision = approve(reviewer, reopening.pk, expected_version=reopening.version)
        assert execute(
            reviewer, reopening.pk, expected_version=decision.proposal.version
        ).executed
        return ActionProposal.objects.get(pk=pending.pk)


@pytest.mark.parametrize("exact", [False, True], ids=["wrong-kind", "exact"])
def test_reopening_invalidates_every_proposal_without_an_exact_marker(
    exact: bool,
) -> None:
    pending = _reopened_with_pending(f"marker-reopen-{exact}", exact=exact)
    invalidated = pending.state == ProposalState.INVALIDATED
    assert invalidated is not exact


def _withdrawal(world: PublicationGraph) -> ActionProposal:
    """Publish for real, then approve a withdrawal of that publication."""
    graph = world.graph
    with tenant_context(graph.tenant.pk):
        assert execute(
            graph.actor, world.proposal.pk, expected_version=world.proposal.version
        ).executed
        cycle = ServiceCycle.objects.get(pk=world.cycle.pk)
        proposal = propose(
            graph.staff,
            cycle.pk,
            ProposalDraft(
                ProposalInput(
                    action_kind=ActionKind.CLIENT_PUBLICATION,
                    step_key="publication-withdrawal",
                    recipient_ids=(world.contact.pk,),
                    subject="Retirada dos documentos",
                    body="Os documentos foram retirados.",
                    withdraw_publication_id=world.proposal.pk,
                    reason="Documento substituído.",
                ),
                timezone.now() + timedelta(days=1),
            ),
            expected_version=cycle.version,
        )
        return approve(
            graph.actor, proposal.pk, expected_version=proposal.version
        ).proposal


@pytest.mark.parametrize("exact", [False, True], ids=["wrong-kind", "exact"])
def test_a_withdrawal_is_rebuilt_as_applied_only_from_its_exact_marker(
    exact: bool,
) -> None:
    world = publication_graph(f"marker-withdrawal-{exact}")
    proposal = _withdrawal(world)
    with tenant_context(world.graph.tenant.pk):
        target = ClientPublication.objects.get(pk=world.proposal.pk)
        ClientPublication.objects.filter(pk=target.pk).update(
            state=PublicationState.WITHDRAWN,
            withdrawn_at=timezone.now(),
            version=target.version + 1,
        )
    marker(proposal, **({} if exact else {"action_kind": ActionKind.EMAIL_NOTICE}))
    if exact:
        with tenant_context(world.graph.tenant.pk):
            assert revalidate(None, proposal.pk).executable
        return
    # Reviewer ruling 2: a wrong receipt is a conflict, refused before any write.
    before = _markers(proposal)
    outcome, statements = _attempt(
        world.graph.tenant.pk, lambda: revalidate(None, proposal.pk)
    )
    assert isinstance(outcome, IntentConflict), outcome
    _no_writes(statements)
    assert _markers(proposal) == before


@pytest.mark.parametrize("exact", [False, True], ids=["wrong-kind", "exact"])
def test_delivery_sends_only_under_an_exact_marker(
    settings: SettingsWrapper, exact: bool
) -> None:
    settings.DELIVERY_PAUSED = False
    setup = reviewable(f"marker-delivery-{exact}")
    proposal = setup.proposal
    tenant = setup.graph.tenant.pk
    marker(proposal, **({} if exact else {"action_kind": ActionKind.REQUEST_WAIVER}))
    with tenant_context(tenant), transaction.atomic():
        _queue(proposal, setup.graph.actor)
    mail.outbox.clear()
    claims = claim_batch(tenant)
    assert len(claims) == 1
    gateway.send(claims[0])
    with tenant_context(tenant):
        message = OutboundMessage.objects.get(pk=claims[0].message_id)
    if exact:
        assert len(mail.outbox) == 1
        assert message.send_started_at is not None
    else:
        assert mail.outbox == []
        assert message.send_started_at is None
        assert (message.state, message.last_error_code) == (
            MessageState.CANCELLED,
            "execution_conflict",
        )
