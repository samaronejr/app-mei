"""Real individual and bulk cards keep internal destination IDs private."""

from collections.abc import Callable
from copy import deepcopy
from datetime import timedelta
from typing import assert_never

import pytest
from django.template.loader import render_to_string
from django.utils import timezone
from playwright.sync_api import expect
from pytest_django.live_server_helper import LiveServer

from apps.accounts.models import User
from apps.clients.models import ClientAssignment
from apps.core.tenancy import tenant_context
from apps.cycles.bulk import BulkKind
from apps.cycles.bulk_contracts import Selection
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.evidence_index import capture_index
from apps.cycles.models import ActionProposal, CycleState
from apps.cycles.services.approvals import propose
from apps.cycles.services.bulk import preview_bulk
from apps.cycles.services.proposed_transitions import (
    propose_completion,
    propose_waiver,
)
from tests.browser.harness import BrowserPage, LiveFirm, assert_policed_roles, axe_check
from tests.cycles.approval_fixtures import approval_graph
from tests.cycles.completion_fixtures import completion_setup
from tests.cycles.cycle_fixtures import CycleSetup
from tests.cycles.publication_fixtures import publication_graph
from tests.portal.request_fixtures import portal_setup
from tests.support import enrol_totp
from tests.ui.factories import Firm

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]


def _scenario(kind: ActionKind) -> tuple[CycleSetup, ActionProposal]:
    """Build an awaiting proposal through the real service for each effect."""
    match kind:
        case ActionKind.EMAIL_NOTICE:
            email = approval_graph("destination-email")
            enrol_totp(email.graph.actor)
            return email.graph, email.proposal
        case ActionKind.CLIENT_PUBLICATION:
            publication = publication_graph("destination-publication")
            with tenant_context(publication.graph.tenant.pk):
                proposal = propose(
                    publication.graph.staff,
                    publication.cycle.pk,
                    ProposalDraft(
                        ProposalInput(
                            action_kind=kind,
                            step_key="destination-publication",
                            recipient_ids=(publication.contact.pk,),
                            subject="Documentos para conferir",
                            body="Confira os documentos no portal.",
                        ),
                        timezone.now() + timedelta(days=1),
                    ),
                    expected_version=publication.cycle.version,
                )
            return publication.graph, proposal
        case ActionKind.CYCLE_COMPLETION:
            completion = completion_setup("destination-completion")
            enrol_totp(completion.graph.actor)
            with tenant_context(completion.graph.tenant.pk):
                capture_index(
                    completion.graph.actor,
                    completion.cycle.pk,
                    expected_version=completion.cycle.version,
                )
                completion.cycle.refresh_from_db()
                proposal = propose_completion(
                    completion.graph.staff,
                    completion.cycle.pk,
                    to_state=CycleState.COMPLETED,
                    reason="",
                    expected_version=completion.cycle.version,
                )
            return completion.graph, proposal
        case ActionKind.REQUEST_WAIVER:
            waiver = portal_setup("destination-waiver")
            with tenant_context(waiver.graph.tenant.pk):
                ClientAssignment.objects.get_or_create(
                    tenant=waiver.graph.tenant,
                    client=waiver.graph.client,
                    user=waiver.graph.staff,
                )
                proposal = propose_waiver(
                    waiver.graph.staff,
                    waiver.request.pk,
                    reason="Dispensa conferida pelo escritório.",
                    expected_version=waiver.request.version,
                )
            return waiver.graph, proposal
        case _:
            assert_never(kind)


@pytest.mark.parametrize("kind", list(ActionKind))
def test_real_cards_show_destinations_without_changing_frozen_effects(
    kind: ActionKind,
    browser_server: LiveServer,
    open_page: OpenPage,
) -> None:
    # Given a real awaiting proposal and an owner allowed to preview its approval.
    graph, proposal = _scenario(kind)
    graph.tenant.features = ["cycles_r1", "bulk_r1"]
    graph.tenant.save(update_fields=["features"])
    live = LiveFirm(
        firm=Firm(
            tenant=graph.tenant,
            owner=graph.actor,
            accountant=graph.staff,
            clients=[graph.client],
        ),
        port=browser_server.thread.port,
    )
    original_bytes, original_digest = bytes(proposal.manifest_bytes), proposal.digest
    label = render_to_string(
        "cycles/_effect_destination.html", {"action_kind": kind}
    ).strip()
    opened = open_page(live, graph.actor)
    page = opened.page

    # When the owner inspects the individual card and its real bulk preview.
    opened.goto(live.url(f"/acoes/{proposal.pk}/"))
    policy = page.locator('section[aria-labelledby="frozen-policy"]')
    expect(policy).to_contain_text(label)
    expect(policy).not_to_contain_text("axorim")
    expect(policy).not_to_contain_text(kind.value)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    axe_check(page)
    opened.screenshot(f"t31-{kind.value}-approval")
    with tenant_context(graph.tenant.pk):
        preview = preview_bulk(
            graph.actor, BulkKind.APPROVE_ITEMS, Selection((proposal.pk,))
        )
        snapshot = deepcopy(preview.snapshot_json)
    opened.goto(live.url(f"/ciclos/lotes/{preview.pk}/"))
    card = page.locator("[data-bulk-approval]")
    expect(card).to_be_visible()
    expect(card).to_contain_text(label)
    expect(card).not_to_contain_text("axorim")
    expect(card).not_to_contain_text(kind.value)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    axe_check(page)
    opened.screenshot(f"t31-{kind.value}-bulk")

    # Then both presentations retain the exact stored effect and frozen preview.
    with tenant_context(graph.tenant.pk):
        proposal.refresh_from_db()
        preview.refresh_from_db()
        assert bytes(proposal.manifest_bytes) == original_bytes
        assert proposal.digest == original_digest
        assert preview.snapshot_json == snapshot
    assert_policed_roles()
    opened.assert_clean_console()
