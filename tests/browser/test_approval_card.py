"""Frozen cards and actual decisions, in both policed live-browser viewports."""

import json
from collections.abc import Callable

import pytest
from playwright.sync_api import expect
from pytest_django.live_server_helper import LiveServer

from apps.accounts.models import User
from apps.clients.models import ClientAssignment
from apps.clients.services import unassign_client
from apps.core.tenancy import tenant_context
from apps.cycles.models import ActionProposal, Approval, CycleEvent, ProposalState
from apps.delivery.models import DeliveryAttempt, DeliveryEffect, OutboundMessage
from tests.browser.harness import (
    BrowserPage,
    LiveFirm,
    assert_policed_roles,
    axe_check,
)
from tests.cycles.approval_fixtures import ApprovalGraph, approval_graph
from tests.support import enrol_totp
from tests.ui.factories import Firm

SCRIPT_BODY = "<script>window.T15_BROWSER_CANARY=true</script>"
OpenPage = Callable[[LiveFirm, User | None], BrowserPage]


@pytest.fixture
def approval_desk(browser_server: LiveServer) -> tuple[ApprovalGraph, LiveFirm]:
    setup = approval_graph("approval-browser", body=SCRIPT_BODY)
    for actor in (setup.graph.actor, setup.graph.staff):
        enrol_totp(actor)
    firm = Firm(
        tenant=setup.graph.tenant,
        owner=setup.graph.actor,
        accountant=setup.graph.staff,
        clients=[setup.graph.client],
    )
    return setup, LiveFirm(firm=firm, port=browser_server.thread.port)


def test_frozen_card_is_accessible_and_keyboard_approval_records_only_a_decision(
    approval_desk: tuple[ApprovalGraph, LiveFirm],
    open_page: OpenPage,
) -> None:
    setup, live = approval_desk
    opened = open_page(live, setup.graph.actor)
    page = opened.page
    url = live.url(f"/acoes/{setup.proposal.pk}/")
    opened.goto(url)
    expect(page.locator("[data-frozen-body]")).to_have_text(SCRIPT_BODY)
    frozen = json.loads(bytes(setup.proposal.manifest_bytes))
    expect(page.locator("[data-frozen-mime-sha256]")).to_have_text(
        frozen["payload"]["mime_sha256"]
    )
    assert page.evaluate("window.T15_BROWSER_CANARY === undefined")
    assert page.evaluate(
        """() => !!(document.querySelector('#frozen-policy').compareDocumentPosition(
        document.querySelector('[data-decision-controls]'))
        & Node.DOCUMENT_POSITION_FOLLOWING)"""
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    axe_check(page)
    headers = page.locator("[data-frozen-mail-headers]")
    summary = headers.locator("summary")
    for _step in range(32):
        page.keyboard.press("Tab")
        if summary.evaluate("(control) => document.activeElement === control"):
            break
    expect(summary).to_be_focused()
    page.keyboard.press("Enter")
    expect(headers).to_have_attribute("open", "")
    expect(headers).to_contain_text(frozen["payload"]["headers"]["Message-ID"])
    opened.screenshot("t15-frozen-mail-headers")
    with tenant_context(setup.graph.tenant.pk):
        deliveries = (
            OutboundMessage.objects.count(),
            DeliveryAttempt.objects.count(),
            DeliveryEffect.objects.count(),
        )
    for _step in range(32):
        page.keyboard.press("Tab")
        if page.locator('button[name="action"][value="approve"]').evaluate(
            "(button) => document.activeElement === button"
        ):
            break
    expect(page.locator('button[value="approve"]')).to_be_focused()
    with page.expect_response(
        lambda response: response.url == url and response.request.method == "POST"
    ) as posted:
        page.keyboard.press("Enter")
    assert posted.value.status == 302
    expect(page.locator("[data-proposal-state]")).to_have_attribute(
        "data-proposal-state", ProposalState.APPROVED
    )
    expect(page.locator("[data-proposal-state] .status-badge")).to_have_attribute(
        "data-state", "manual"
    )
    with tenant_context(setup.graph.tenant.pk):
        decision = Approval.objects.get(proposal=setup.proposal)
        assert decision.digest == setup.proposal.digest
        assert (
            OutboundMessage.objects.count(),
            DeliveryAttempt.objects.count(),
            DeliveryEffect.objects.count(),
        ) == deliveries
    opened.assert_clean_console()
    opened.screenshot("t15-keyboard-approved")


def test_stale_real_browser_form_is_409_without_a_second_decision(
    approval_desk: tuple[ApprovalGraph, LiveFirm],
    open_page: OpenPage,
) -> None:
    setup, live = approval_desk
    current, stale = (
        open_page(live, setup.graph.actor),
        open_page(live, setup.graph.actor),
    )
    url = live.url(f"/acoes/{setup.proposal.pk}/")
    current.goto(url)
    stale.goto(url)
    current.page.locator('button[value="approve"]').click()
    expect(current.page.locator("[data-proposal-state]")).to_have_attribute(
        "data-proposal-state", ProposalState.APPROVED
    )
    expect(
        current.page.locator("[data-proposal-state] .status-badge")
    ).to_have_attribute("data-state", "manual")
    with tenant_context(setup.graph.tenant.pk):
        event_count = CycleEvent.objects.count()
    stale.page.get_by_label("Motivo para não aprovar").fill("A stale submitted reason.")
    with stale.page.expect_response(
        lambda response: response.url == url and response.request.method == "POST"
    ) as posted:
        stale.page.locator('button[value="reject"]').click()
    assert posted.value.status == 409
    expect(stale.page.get_by_role("alert")).to_be_visible()
    expect(stale.page.locator("[data-proposal-state]")).to_have_attribute(
        "data-proposal-state", ProposalState.APPROVED
    )
    expect(stale.page.locator("[data-proposal-state] .status-badge")).to_have_attribute(
        "data-state", "manual"
    )
    with tenant_context(setup.graph.tenant.pk):
        assert Approval.objects.filter(proposal=setup.proposal).count() == 1
        assert CycleEvent.objects.count() == event_count
    assert_policed_roles()
    current.assert_clean_console()
    assert all("status of 409" in error for error in stale.console_errors)
    stale.screenshot("t15-stale-409")


def test_reject_then_revise_and_cancel_preserves_the_original_decision(
    approval_desk: tuple[ApprovalGraph, LiveFirm],
    open_page: OpenPage,
) -> None:
    setup, live = approval_desk
    opened = open_page(live, setup.graph.actor)
    page = opened.page
    opened.goto(live.url(f"/acoes/{setup.proposal.pk}/"))
    page.get_by_label("Motivo para não aprovar").fill("Needs another human review.")
    page.locator('button[value="reject"]').click()
    expect(page.locator("[data-proposal-state]")).to_have_attribute(
        "data-proposal-state", ProposalState.REJECTED
    )
    expect(page.locator("[data-proposal-state] .status-badge")).to_have_attribute(
        "data-state", "blocked"
    )
    page.locator('a[href*="revisao_de="]').click()
    expect(page.locator("[data-proposal-form]")).to_be_visible()
    expect(page.locator('input[name="recipients"]:checked')).to_have_value(
        str(setup.contact.pk)
    )
    page.locator('[data-proposal-form] button[type="submit"]').click()
    expect(page.locator("[data-approval-card]")).to_be_visible()
    new_id = page.locator("[data-approval-card]").get_attribute("data-proposal-id")
    assert new_id is not None
    assert new_id != str(setup.proposal.pk)
    expect(page.locator('button[value="approve"]')).to_have_count(0)
    page.locator('button[value="cancel"]').click()
    expect(page.locator("[data-proposal-state]")).to_have_attribute(
        "data-proposal-state", ProposalState.CANCELLED
    )
    expect(page.locator("[data-proposal-state] .status-badge")).to_have_attribute(
        "data-state", "blocked"
    )
    with tenant_context(setup.graph.tenant.pk):
        original = Approval.objects.get(proposal=setup.proposal)
        revision = ActionProposal.objects.get(pk=new_id)
        assert original.digest == setup.proposal.digest
        assert revision.previous_id == setup.proposal.pk
        assert revision.revision == 2
        assert not Approval.objects.filter(proposal=revision).exists()
    assert_policed_roles()
    opened.assert_clean_console()


def test_staff_policy_refusal_and_revoked_portfolio_show_no_private_card(
    approval_desk: tuple[ApprovalGraph, LiveFirm],
    open_page: OpenPage,
) -> None:
    setup, live = approval_desk
    opened = open_page(live, setup.graph.staff)
    page = opened.page
    url = live.url(f"/acoes/{setup.proposal.pk}/")
    opened.goto(url)
    expect(page.locator('button[value="approve"]')).to_have_count(0)
    denied = page.goto(live.url("/gestao/aprovacoes/"))
    assert denied is not None
    assert denied.status == 403
    with tenant_context(setup.graph.tenant.pk):
        assignment = ClientAssignment.objects.get(
            client=setup.graph.client, user=setup.graph.staff
        )
        unassign_client(actor=setup.graph.actor, assignment=assignment)
    invisible = page.goto(url)
    assert invisible is not None
    assert invisible.status == 404
    expect(page.locator("[data-approval-card]")).to_have_count(0)
    assert "recipient@example.test" not in page.locator("body").inner_text()
    assert_policed_roles()
    assert all(
        "status of 403" in error or "status of 404" in error
        for error in opened.console_errors
    )
    with tenant_context(setup.graph.tenant.pk):
        assert not Approval.objects.filter(proposal=setup.proposal).exists()
