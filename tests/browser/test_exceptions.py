"""T14: the drawer opens and acts by keyboard; the portal sees only its sentence."""

import logging
from collections.abc import Callable
from datetime import date
from uuid import uuid4

import pytest
from playwright.sync_api import Locator, Page, expect

from apps.accounts.models import User
from apps.clients.models import ClientAssignment, ClientCompany, Engagement, TaxProfile
from apps.core.tenancy import tenant_context
from apps.cycles.models import Exception as CycleException
from apps.cycles.models import ExceptionState, PlaybookVersion, ServiceCycle
from apps.cycles.openers import CERTIFICATE_EXPIRING
from apps.cycles.services.cycle import open_cycle
from apps.cycles.services.exceptions import assign_exception, open_exception
from apps.tenants.models import TenantRole
from tests.browser.harness import (
    BrowserPage,
    LiveFirm,
    assert_policed_roles,
    axe_check,
    mfa_session_cookie,
)
from tests.ui.factories import add_client, add_member

pytestmark = pytest.mark.browser
logger = logging.getLogger(__name__)

PERIOD = date(2026, 9, 1)


def _cycle_with_exceptions(live: LiveFirm) -> tuple[ClientCompany, ServiceCycle]:
    client = add_client(
        live.firm, legal_name="Oficina Gama Sintética", base="112223330001"
    )
    with tenant_context(live.firm.tenant.pk):
        engagement = Engagement.objects.create(
            tenant=live.firm.tenant,
            client=client,
            service_kind="mei_mensal",
            starts_on=PERIOD,
            responsible=live.firm.owner,
            provenance="staff_entry",
        )
        TaxProfile.objects.create(
            tenant=live.firm.tenant,
            client=client,
            regime="mei",
            valid_from=PERIOD,
            provenance="staff_entry",
        )
        definition = PlaybookVersion.objects.get(
            playbook__key="mei-mensal", state="published"
        )
        ref = open_cycle(
            live.firm.owner,
            engagement.pk,
            PERIOD,
            definition.pk,
            idempotency_key="t14-browser",
        )
        for version, (code, subject) in enumerate(
            ((CERTIFICATE_EXPIRING, client.pk), ("delivery_unknown", uuid4())),
            start=ref.version,
        ):
            open_exception(
                code,
                subject,
                actor=live.firm.owner,
                cycle_id=ref.id,
                expected_version=version,
                owner=live.firm.owner,
                review_on=PERIOD,
            )
        return client, ServiceCycle.objects.get(pk=ref.id)


def _tab_to(page: Page, target: Locator) -> None:
    """Real sequential keyboard input, not programmatically assigned focus."""
    for _ in range(120):
        if target.evaluate("(element) => element === document.activeElement"):
            return
        page.keyboard.press("Tab")
    pytest.fail("Target was not reachable in 120 sequential Tab presses.")


def _open_and_start(page: BrowserPage, uncertain: Locator, selected_url: str) -> None:
    opener = uncertain.locator("a[aria-haspopup='dialog']")
    _tab_to(page.page, opener)
    with page.page.expect_navigation(
        url=selected_url, wait_until="load", timeout=10000
    ):
        page.page.keyboard.press("Enter")
    dialog = uncertain.locator("dialog")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("button", name="Fechar", exact=True)).to_be_focused()
    page.page.reload(wait_until="load")
    expect(page.page).to_have_url(selected_url)
    expect(dialog.get_by_role("button", name="Fechar", exact=True)).to_be_focused()
    page.page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(opener).to_be_focused()
    expect(page.page).not_to_have_url(selected_url)
    with page.page.expect_navigation(
        url=selected_url, wait_until="load", timeout=10000
    ):
        page.page.keyboard.press("Enter")
    _tab_to(page.page, dialog.get_by_role("button", name="Fechar", exact=True))
    page.page.keyboard.press("Enter")
    expect(opener).to_be_focused()
    with page.page.expect_navigation(
        url=selected_url, wait_until="load", timeout=10000
    ):
        page.page.keyboard.press("Enter")
    start = uncertain.get_by_role("button", name="Começar a tratar", exact=True)
    _tab_to(page.page, start)
    with (
        page.page.expect_response(
            lambda response: response.request.method == "POST",
            timeout=10000,
        ) as posting,
        page.page.expect_navigation(url=selected_url, wait_until="load", timeout=10000),
    ):
        page.page.keyboard.press("Enter")
    assert posting.value.status == 302
    expect(uncertain).to_contain_text("Em tratamento")
    expect(uncertain).to_contain_text("não pode ser aceita como risco")
    expect(
        uncertain.get_by_role("button", name="Aceitar como risco", exact=True)
    ).to_have_count(0)
    expect(uncertain.get_by_label("O que foi feito ou motivo")).to_be_visible()


def _finish_with_conflict(
    page: BrowserPage, live: LiveFirm, identifier: str, selected_url: str
) -> None:
    dialog = page.page.locator(f"#exception-{identifier}")
    note = dialog.get_by_label("O que foi feito ou motivo")
    _tab_to(page.page, note)
    page.page.keyboard.type("Synthetic stale resolution attempt.")
    # Advance through the real service while the rendered form keeps revision 2.
    with tenant_context(live.firm.tenant.pk):
        problem = CycleException.objects.get(pk=identifier)
        ClientAssignment.objects.create(
            tenant=live.firm.tenant,
            client=problem.client,
            user=live.firm.accountant,
        )
        advanced = assign_exception(
            live.firm.owner,
            problem.pk,
            live.firm.accountant.pk,
            expected_version=2,
        )
        assert advanced.version == 3
    resolve = dialog.get_by_role("button", name="Marcar como resolvida", exact=True)
    _tab_to(page.page, resolve)
    with page.page.expect_navigation(wait_until="load", timeout=10000) as stale:
        page.page.keyboard.press("Enter")
    assert stale.value is not None
    assert stale.value.status == 409
    expect(page.page).to_have_url(selected_url)
    error = dialog.locator("[role='alert']")
    expect(error).to_be_focused()
    logger.info(
        "T14 keyboard receipt: %s",
        {
            "event": "stale POST load",
            "viewport": page.page.viewport_size,
            "url": page.page.url,
            "status": stale.value.status,
            "focus": error.get_attribute("id"),
        },
    )
    _tab_to(page.page, resolve)
    with page.page.expect_navigation(wait_until="load", timeout=10000) as invalid:
        page.page.keyboard.press("Enter")
    assert invalid.value is not None
    assert invalid.value.status == 422
    expect(page.page).to_have_url(selected_url)
    expect(error).to_be_focused()
    logger.info(
        "T14 keyboard receipt: %s",
        {
            "event": "invalid POST load",
            "viewport": page.page.viewport_size,
            "url": page.page.url,
            "status": invalid.value.status,
            "focus": error.get_attribute("id"),
        },
    )
    note = dialog.get_by_label("O que foi feito ou motivo")
    _tab_to(page.page, note)
    page.page.keyboard.type("Synthetic verified resolution.")
    _tab_to(page.page, resolve)
    with page.page.expect_navigation(
        url=selected_url, wait_until="load", timeout=10000
    ):
        page.page.keyboard.press("Enter")
    expect(dialog.locator("[data-state]")).to_have_attribute("data-state", "resolvida")
    expect(dialog.get_by_role("button", name="Fechar", exact=True)).to_be_focused()
    page.page.reload(wait_until="load")
    expect(dialog.locator("[data-state]")).to_have_attribute("data-state", "resolvida")
    logger.info(
        "T14 keyboard receipt: %s",
        {
            "event": "terminal POST and reload",
            "viewport": page.page.viewport_size,
            "url": page.page.url,
            "state": dialog.locator("[data-state]").get_attribute("data-state"),
            "focus_inside": dialog.evaluate(
                "(element) => element.contains(document.activeElement)"
            ),
        },
    )


@pytest.mark.parametrize("origin", ["ciclo", "cliente"])
def test_exception_drawer_is_keyboard_operable_and_portal_sees_only_its_sentence(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
    origin: str,
) -> None:
    live = live_firm()
    client, cycle = _cycle_with_exceptions(live)
    page = open_page(live, None)
    base = (
        f"/ciclos/{cycle.pk}/?"
        if origin == "ciclo"
        else f"/clientes/{client.pk}/?periodo=2026-09&"
    )
    page.goto(live.url(base))
    section = page.page.locator("#pendencias")
    drawers = section.locator("[data-exception-id]")
    expect(drawers).to_have_count(2)
    uncertain = section.locator("[data-reason='delivery_unknown']")
    identifier = uncertain.get_attribute("data-exception-id")
    assert identifier is not None
    selected_url = live.url(f"{base}pendencia={identifier}#pendencias")
    _open_and_start(page, uncertain, selected_url)
    _finish_with_conflict(page, live, identifier, selected_url)
    assert page.page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth"
    )
    axe_check(page.page)
    screenshot = page.screenshot(f"AXR1-014-drawer-{origin}")
    if screenshot is not None:
        page.page.screenshot(
            path=str(screenshot.with_stem(f"{screenshot.stem}-viewport"))
        )
    assert page.console_errors == [
        "Failed to load resource: the server responded with a status of 409 (Conflict)",
        (
            "Failed to load resource: the server responded with a status of 422 "
            "(Unprocessable Content)"
        ),
    ]
    with tenant_context(live.firm.tenant.pk):
        assert (
            CycleException.objects.get(reason_code="delivery_unknown").state
            == ExceptionState.RESOLVED
        )

    portal_user = add_member(
        live.firm.tenant,
        "dona@gama.example.com",
        TenantRole.CLIENT_OWNER,
        client=client,
    )
    portal = open_page(live, portal_user)
    portal.page.context.add_cookies(
        [mfa_session_cookie(portal_user, live.url("/", portal=True))]
    )
    portal.goto(live.url("/", portal=True))
    main = portal.page.locator("main")
    expect(main).to_contain_text("Precisamos da sua ajuda")
    expect(main).to_contain_text("Seu certificado digital vence em breve")
    expect(main).not_to_contain_text("Verifique o envio antes de decidir")
    expect(main).not_to_contain_text("Avise o cliente")
    axe_check(portal.page)
    portal.screenshot("AXR1-014-portal-help")
    portal.assert_clean_console()
    assert_policed_roles()
