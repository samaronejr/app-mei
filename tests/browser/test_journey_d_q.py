"""S15 D and Q in a real browser: an approval that lost its basis is visibly void.

Q -- the recipient changes while the card is open: approving the stale card answers
409, records no decision, and the card shows the version invalidated with its reason,
the stale alert, and the way to a new version.

D -- authority revoked between approval and execution: the reviewer approves, the
owner withdraws the review grant on the team page, and "Executar" refuses, voids the
version and queues nothing.

Both viewports. No fixed sleep: every wait is an auto-waiting `expect` or a response.
"""

from collections.abc import Callable

from django.urls import reverse
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.accounts.models import User
from apps.clients.maintenance import ContactData, save_contact
from apps.core.tenancy import tenant_context
from apps.cycles.approval_views import REASON_LABELS
from apps.cycles.models import ActionProposal, Approval, ProposalState
from apps.delivery.models import OutboundMessage
from apps.tenants.models import Membership
from tests.browser.harness import BrowserPage, LiveFirm, assert_policed_roles
from tests.browser.journeys import live_graph, record
from tests.cycles.approval_fixtures import ApprovalGraph, approval_graph

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]
CHANGED = "changed@journey-dq.test"


def _desk(server: LiveServer, slug: str) -> tuple[ApprovalGraph, LiveFirm]:
    setup = approval_graph(slug)
    return setup, live_graph(setup.graph, server.thread.port)


def _post(page: Page, url: str, button: str) -> int:
    with page.expect_response(
        lambda response: response.url == url and response.request.method == "POST"
    ) as posted:
        page.locator(f'button[name="action"][value="{button}"]').click()
    return posted.value.status


def _decision_in_view(page: Page) -> bool:
    page.locator("[data-identity-block]").scroll_into_view_if_needed()
    return bool(
        page.evaluate(
            """() => {
            const box = document.querySelector('[data-decision-controls]')
              .getBoundingClientRect();
            return box.top >= 0 && box.top < innerHeight && scrollY > 0;
        }"""
        )
    )


def test_journey_q_recipient_change_voids_the_open_card(
    browser_server: LiveServer, open_page: OpenPage
) -> None:
    setup, live = _desk(browser_server, "journey-q")
    owner = open_page(live, setup.graph.actor)
    page = owner.page
    url = live.url(f"/acoes/{setup.proposal.pk}/")
    owner.goto(url)
    expect(page.locator("[data-proposal-state]")).to_have_attribute(
        "data-proposal-state", ProposalState.AWAITING
    )
    expect(page.locator("[data-proposal-state] .status-badge")).to_have_attribute(
        "data-state", "pending"
    )
    expect(page.locator("[data-frozen-recipient]")).to_contain_text(
        "recipient@example.test"
    )
    if owner.viewport == "desktop":
        assert _decision_in_view(page), "decision area scrolled out of view"
    record(owner, "q-01-card-awaiting")

    with tenant_context(setup.graph.tenant.pk):
        save_contact(
            actor=setup.graph.actor,
            tenant_id=setup.graph.tenant.pk,
            client_id=setup.graph.client.pk,
            contact_id=setup.contact.pk,
            data=ContactData(name=setup.contact.name, email=CHANGED),
            expected_version=setup.contact.version,
        )
    assert _post(page, url, "approve") == 409
    alert = page.get_by_role("alert")
    expect(alert).to_have_attribute("data-approval-alert", "conflict")
    expect(page.locator("[data-proposal-state]")).to_have_attribute(
        "data-proposal-state", ProposalState.INVALIDATED
    )
    expect(page.locator("[data-proposal-state] .status-badge")).to_have_attribute(
        "data-state", "stale"
    )
    with tenant_context(setup.graph.tenant.pk):
        voided = ActionProposal.objects.get(pk=setup.proposal.pk)
        assert not Approval.objects.filter(proposal=voided).exists()
    assert voided.invalidation_reasons
    warnings = page.locator('section[aria-labelledby="approval-warnings"]')
    for code in voided.invalidation_reasons:
        expect(warnings).to_contain_text(REASON_LABELS[code])
    expect(page.locator('button[value="approve"]')).to_have_count(0)
    record(owner, "q-02-invalidated")

    page.get_by_role("link", name="Criar nova versão com efeitos atualizados").click()
    form = page.locator("[data-proposal-form]")
    expect(form).to_be_visible()
    form.get_by_role("button", name="Congelar efeitos e conferir cartão").click()
    expect(page.locator("[data-frozen-recipient]")).to_contain_text(CHANGED)
    expect(page.locator("[data-proposal-state]")).to_have_attribute(
        "data-proposal-state", ProposalState.AWAITING
    )
    expect(page.locator("[data-proposal-state] .status-badge")).to_have_attribute(
        "data-state", "pending"
    )
    record(owner, "q-03-new-version")
    assert_policed_roles()
    assert all("status of 409" in error for error in owner.console_errors), (
        owner.console_errors
    )


def test_journey_d_revoked_reviewer_cannot_have_the_approval_executed(
    browser_server: LiveServer, open_page: OpenPage
) -> None:
    setup, live = _desk(browser_server, "journey-d")
    owner = open_page(live, setup.graph.actor)
    staff = open_page(live, setup.graph.staff)
    member = Membership.objects.get(
        tenant=setup.graph.tenant, user=setup.graph.staff, client__isnull=True
    )
    toggle = owner.page.locator(
        f'form[action="{reverse("member-review-toggle", args=[member.pk])}"]'
    ).get_by_role("button")
    owner.goto(live.url(reverse("team")))
    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "true")

    owner.goto(live.url(f"/acoes/{setup.proposal.pk}/"))
    owner.page.get_by_role(
        "link", name="Criar nova versão com efeitos atualizados"
    ).click()
    owner.page.locator("[data-proposal-form]").get_by_role(
        "button", name="Congelar efeitos e conferir cartão"
    ).click()
    card = owner.page.locator("[data-approval-card]")
    expect(card).to_be_visible()
    revision = card.get_attribute("data-proposal-id")
    assert revision is not None
    assert revision != str(setup.proposal.pk)
    url = live.url(f"/acoes/{revision}/")

    staff.goto(url)
    assert _post(staff.page, url, "approve") == 302
    expect(staff.page.locator("[data-proposal-state]")).to_have_attribute(
        "data-proposal-state", ProposalState.APPROVED
    )
    record(staff, "d-01-reviewer-approved")

    owner.goto(live.url(reverse("team")))
    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "false")

    owner.goto(url)
    expect(owner.page.locator('button[value="execute"]')).to_be_visible()
    assert _post(owner.page, url, "execute") == 409
    expect(owner.page.get_by_role("alert")).to_have_attribute(
        "data-approval-alert", "conflict"
    )
    expect(owner.page.locator("[data-proposal-state]")).to_have_attribute(
        "data-proposal-state", ProposalState.INVALIDATED
    )
    expect(owner.page.locator("[data-proposal-state] .status-badge")).to_have_attribute(
        "data-state", "stale"
    )
    expect(owner.page.locator('button[value="execute"]')).to_have_count(0)
    with tenant_context(setup.graph.tenant.pk):
        voided = ActionProposal.objects.get(pk=revision)
        assert Approval.objects.filter(proposal=voided).count() == 1
        assert not OutboundMessage.objects.exists()
    assert any(code.startswith("approver") for code in voided.invalidation_reasons)
    warnings = owner.page.locator('section[aria-labelledby="approval-warnings"]')
    for code in voided.invalidation_reasons:
        expect(warnings).to_contain_text(REASON_LABELS[code])
    record(owner, "d-02-execution-refused")
    assert_policed_roles()
    staff.assert_clean_console()
    assert all("status of 409" in error for error in owner.console_errors), (
        owner.console_errors
    )
