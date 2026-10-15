"""Real HTTP previews distinguish frozen closure from an unexecuted amendment."""

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from html import unescape
from http import HTTPStatus
from typing import assert_never

import pytest
from django.test import Client
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.cycles.bulk import BulkKind, BulkOperation
from apps.cycles.effects.executors import execute
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind, canonicalize
from apps.cycles.evidence_index import capture_index
from apps.cycles.models import (
    ActionExecution,
    ActionProposal,
    Approval,
    CompletionGeneration,
    CycleAmendment,
    CycleState,
    ProposalState,
)
from apps.cycles.services.approvals import approve, propose
from apps.cycles.services.proposed_transitions import propose_completion, propose_waiver
from apps.cycles.services.requests import RequestFields, create_request
from apps.tenants.models import Membership, TenantRole
from tests.browser.harness import mfa_session_cookie
from tests.cycles.completion_fixtures import CompletionSetup, completion_setup
from tests.support import enrol_totp
from tests.ui.factories import add_member

pytestmark = pytest.mark.django_db(transaction=True)


@dataclass(frozen=True, slots=True)
class Desk:
    setup: CompletionSetup
    owner: Client
    reviewer: Client


@pytest.fixture
def desk(settings: SettingsWrapper) -> Desk:
    settings.ALLOWED_HOSTS = ["localhost", "127.0.0.1", ".localhost"]
    setup = completion_setup("bulk-disclosure-http")
    graph = setup.graph
    graph.tenant.features = ["cycles_r1", "bulk_r1"]
    graph.tenant.save(update_fields=["features"])
    enrol_totp(graph.actor)
    reviewer = add_member(
        graph.tenant, "reviewer@bulk-disclosure.test", TenantRole.OPERATIONS_ADMIN
    )
    host = f"{graph.tenant.slug}.localhost"
    owner_session, reviewer_session = Client(SERVER_NAME=host), Client(SERVER_NAME=host)
    for user, session in (
        (graph.actor, owner_session),
        (reviewer, reviewer_session),
    ):
        cookie = mfa_session_cookie(user, f"http://{host}/")
        assert "name" in cookie
        assert "value" in cookie
        session.cookies[cookie["name"]] = cookie["value"]
    return Desk(setup, owner_session, reviewer_session)


def preview(session: Client, proposal: ActionProposal) -> BulkOperation:
    response = session.post(
        "/ciclos/lotes/",
        {"kind": BulkKind.APPROVE_ITEMS, "subject_ids": [str(proposal.pk)]},
    )
    assert response.status_code == HTTPStatus.FOUND
    with tenant_context(proposal.tenant_id):
        operation = BulkOperation.objects.get(
            pk=response.headers["Location"].strip("/").split("/")[-1]
        )
        assert operation.snapshot_json["items"][0]["eligible"] is True
    return operation


def action_labels(page: str) -> list[tuple[str, str]]:
    """Read the semantic value and visible text of heading/destination labels."""
    return [
        (kind, unescape(label).strip())
        for kind, label in re.findall(
            r'<span data-bulk-action="([^"]+)">([^<]+)</span>', page
        )
    ]


@pytest.fixture
def frozen_previews(
    desk: Desk,
) -> tuple[BulkOperation, BulkOperation, ActionProposal, CompletionGeneration]:
    """Preview an awaiting completion, execute it, then propose reopening over HTTP."""
    setup, graph = desk.setup, desk.setup.graph
    with tenant_context(graph.tenant.pk):
        capture_index(graph.actor, setup.cycle.pk, expected_version=setup.cycle.version)
        setup.cycle.refresh_from_db()
        completion = propose_completion(
            graph.staff,
            setup.cycle.pk,
            to_state=CycleState.COMPLETED,
            reason="",
            expected_version=setup.cycle.version,
        )
    completion_preview = preview(desk.reviewer, completion)
    with tenant_context(graph.tenant.pk):
        approved = approve(
            graph.actor, completion.pk, expected_version=completion.version
        ).proposal
        result = execute(graph.actor, approved.pk, expected_version=approved.version)
        assert result.executed
        setup.cycle.refresh_from_db()
        generation = CompletionGeneration.objects.get(cycle=setup.cycle)
    reopened = desk.owner.post(
        f"/ciclos/{setup.cycle.pk}/reabrir/",
        {"expected_version": setup.cycle.version, "reason": "AMENDMENT-046"},
    )
    assert reopened.status_code == HTTPStatus.FOUND
    with tenant_context(graph.tenant.pk):
        proposal = ActionProposal.objects.get(
            pk=reopened.headers["Location"].strip("/").split("/")[-1]
        )
        assert proposal.initiator_id == graph.actor.pk
        assert proposal.state == ProposalState.AWAITING
        frozen = json.loads(bytes(proposal.manifest_bytes))
        assert frozen["payload"]["reopen"] == {
            "generation_id": str(generation.pk),
            "affected_scope": [str(setup.cycle.pk)],
        }
    reopen_preview = preview(desk.reviewer, proposal)
    return completion_preview, reopen_preview, proposal, generation


def test_http_reopening_discloses_preserved_scope_and_downstream_invalidation(
    desk: Desk,
    frozen_previews: tuple[
        BulkOperation, BulkOperation, ActionProposal, CompletionGeneration
    ],
) -> None:
    # Given an actual frozen completion and its awaiting reopening by another actor.
    completion_preview, reopen_preview, _, generation = frozen_previews

    # When the stored previews are read after the live cycle has already closed.
    closed_page = desk.reviewer.get(f"/ciclos/lotes/{completion_preview.pk}/")
    reopen_page = desk.reviewer.get(f"/ciclos/lotes/{reopen_preview.pk}/")

    # Then actual visible labels and structural effects distinguish the same kind.
    assert closed_page.status_code == reopen_page.status_code == HTTPStatus.OK
    closed_html = closed_page.content.decode()
    reopen_html = reopen_page.content.decode()
    closed_labels = action_labels(closed_html)
    reopen_labels = action_labels(reopen_html)
    assert [kind for kind, _ in closed_labels] == ["cycle_completion"] * 2
    assert [kind for kind, _ in reopen_labels] == ["reopen"] * 2
    assert closed_labels[0][1]
    assert reopen_labels[0][1]
    assert closed_labels[0][1] != reopen_labels[0][1]
    assert closed_labels[0] == closed_labels[1]
    assert reopen_labels[0] == reopen_labels[1]
    assert 'data-bulk-completion-state="concluido"' in closed_html
    assert "data-bulk-generation" not in closed_html
    assert "data-bulk-completion-state" not in reopen_html
    generations = re.findall(
        r"<code[^>]* data-bulk-generation>([^<]+)</code>", reopen_html
    )
    assert generations == [str(generation.pk)]
    assert re.findall(r"<li[^>]* data-bulk-scope>([^<]+)</li>", reopen_html) == [
        str(desk.setup.cycle.pk)
    ]
    assert (
        'data-bulk-invalidation="cycle_amended" '
        'data-proposal-states="aguardando_aprovacao aprovada" '
        'data-executed="false"'
    ) in reopen_html


def test_http_disclosure_reads_preserve_frozen_bytes_and_record_no_decision(
    desk: Desk,
    frozen_previews: tuple[
        BulkOperation, BulkOperation, ActionProposal, CompletionGeneration
    ],
) -> None:
    # Given the exact stored identities and current execution/decision counts.
    completion_preview, reopen_preview, proposal, _ = frozen_previews
    setup, graph = desk.setup, desk.setup.graph
    with tenant_context(graph.tenant.pk):
        before = (bytes(proposal.manifest_bytes), proposal.digest, proposal.version)
        snapshots = [
            (canonicalize(row.snapshot_json), row.snapshot_digest, row.idempotency_key)
            for row in (completion_preview, reopen_preview)
        ]
        counts = (Approval.objects.count(), ActionExecution.objects.count())

    # When the independent reviewer reads both existing previews.
    for row in (completion_preview, reopen_preview):
        response = desk.reviewer.get(f"/ciclos/lotes/{row.pk}/")
        assert response.status_code == HTTPStatus.OK

    # Then reads neither rewrite frozen identities nor approve/execute the amendment.
    with tenant_context(graph.tenant.pk):
        proposal.refresh_from_db()
        setup.cycle.refresh_from_db()
        assert (
            bytes(proposal.manifest_bytes),
            proposal.digest,
            proposal.version,
        ) == before
        assert proposal.state == ProposalState.AWAITING
        assert (setup.cycle.state, setup.cycle.amendment) == (CycleState.COMPLETED, 0)
        assert not CycleAmendment.objects.exists()
        assert (Approval.objects.count(), ActionExecution.objects.count()) == counts
        for row, snapshot in zip(
            (completion_preview, reopen_preview), snapshots, strict=True
        ):
            row.refresh_from_db()
            assert (
                canonicalize(row.snapshot_json),
                row.snapshot_digest,
                row.idempotency_key,
            ) == snapshot


@pytest.mark.parametrize(
    ("kind", "state"),
    [
        (ActionKind.EMAIL_NOTICE, ""),
        (ActionKind.CLIENT_PUBLICATION, ""),
        (ActionKind.REQUEST_WAIVER, ""),
        (ActionKind.CYCLE_COMPLETION, CycleState.COMPLETED),
        (ActionKind.CYCLE_COMPLETION, CycleState.WITH_PENDING),
    ],
)
def test_http_other_frozen_actions_never_disclose_reopening(
    desk: Desk, kind: ActionKind, state: str
) -> None:
    # Given a real awaiting proposal for each other supported frozen effect.
    setup, graph = desk.setup, desk.setup.graph
    reasons: dict[str, str] = {}
    with tenant_context(graph.tenant.pk):
        match kind:
            case ActionKind.EMAIL_NOTICE | ActionKind.CLIENT_PUBLICATION:
                recipient = add_member(
                    graph.tenant,
                    "recipient@bulk-disclosure.test",
                    TenantRole.CLIENT_OWNER,
                    client=graph.client,
                )
                contact = ClientContact.objects.create(
                    tenant=graph.tenant,
                    client=graph.client,
                    membership=Membership.objects.get(
                        tenant=graph.tenant, user=recipient
                    ),
                    name="Recipient",
                    email=recipient.email,
                )
                proposal = propose(
                    graph.staff,
                    setup.cycle.pk,
                    ProposalDraft(
                        ProposalInput(
                            action_kind=kind,
                            step_key="disclosure",
                            recipient_ids=(contact.pk,),
                            subject="SUBJECT-046",
                            body="BODY-046",
                        ),
                        timezone.now() + timedelta(days=1),
                    ),
                    expected_version=setup.cycle.version,
                )
            case ActionKind.REQUEST_WAIVER:
                request = create_request(
                    graph.actor,
                    setup.cycle.pk,
                    RequestFields("REQUEST-046"),
                    expected_version=setup.cycle.version,
                )
                proposal = propose_waiver(
                    graph.staff,
                    request.pk,
                    reason="WAIVER-046",
                    expected_version=request.version,
                )
            case ActionKind.CYCLE_COMPLETION:
                if state == CycleState.WITH_PENDING:
                    request = create_request(
                        graph.actor,
                        setup.cycle.pk,
                        RequestFields("PENDING-046"),
                        expected_version=setup.cycle.version,
                    )
                    setup.cycle.refresh_from_db()
                    reasons[f"requests:{request.pk}:{request.version}"] = "REASON-046"
                capture_index(
                    graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
                )
                setup.cycle.refresh_from_db()
                proposal = propose_completion(
                    graph.staff,
                    setup.cycle.pk,
                    to_state=state,
                    reason=reasons,
                    expected_version=setup.cycle.version,
                )
            case _:
                assert_never(kind)
    operation = preview(desk.reviewer, proposal)

    # When the authorized, independent reviewer reads the frozen HTTP preview.
    response = desk.reviewer.get(f"/ciclos/lotes/{operation.pk}/")

    # Then heading/destination branches and closure details match input semantics.
    assert response.status_code == HTTPStatus.OK
    page = response.content.decode()
    labels = action_labels(page)
    assert [value for value, _ in labels] == [kind.value] * 2
    assert labels[0] == labels[1]
    assert labels[0][1]
    assert "data-bulk-generation" not in page
    assert "data-bulk-invalidation" not in page
    if state:
        assert f'data-bulk-completion-state="{state}"' in page
        assert re.findall(r'data-bulk-reason-key="([^"]+)"', page) == list(reasons)
    else:
        assert "data-bulk-completion-state" not in page
