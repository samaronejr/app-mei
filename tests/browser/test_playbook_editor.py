"""The playbook editor in a real browser: keyboard reorder, axe, both viewports.

The contract under test is WCAG 2.5.7 — reordering must be possible without a
drag gesture. Each step carries up/down submit buttons, so a keyboard press is a
real POST and the whole reorder happens with no JavaScript and no pointer. The
editor underneath a flagged firm runs as `app_runtime`, which `goto` and
`assert_policed_roles` prove on every navigation.
"""

from collections.abc import Callable

import pytest
from playwright.sync_api import expect

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.cycles.models import Playbook
from apps.cycles.services import playbooks
from tests.browser.harness import (
    BrowserPage,
    LiveFirm,
    assert_policed_roles,
    axe_check,
)

pytestmark = pytest.mark.browser

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]


def _clone_pack(live: LiveFirm) -> Playbook:
    """Give the firm an editable draft of the MEI pack to reorder.

    The flag fixture's `Tenant.save` fired the install signal, so the pack rows
    already exist; cloning is also the only legitimate way to open a system
    pack's step list in the editor.
    """
    with tenant_context(live.firm.tenant.id):
        system = Playbook.objects.get(key="mei-mensal")
        return playbooks.clone_playbook(
            actor=live.firm.owner,
            tenant=live.firm.tenant,
            playbook_pk=system.pk,
        )


def test_keyboard_reorder_moves_the_step_and_stays_accessible(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    live = live_firm()
    playbook = _clone_pack(live)
    page = open_page(live, None)

    page.goto(live.url(f"/gestao/playbooks/{playbook.pk}/v/1/"))

    first_key = page.page.locator("input[name='steps-0-key']")
    second_key = page.page.locator("input[name='steps-1-key']")
    expect(first_key).to_have_value("notas-fiscais")
    expect(second_key).to_have_value("extrato-receitas")

    # Keyboard, not pointer: focus the named button and press Enter. The names
    # are the aria-labels the template writes — if they ever vanish, this is
    # also the axe "button without a name" failure.
    move_down = page.page.get_by_role("button", name="Mover etapa 1 para baixo")
    expect(move_down).to_be_visible()
    move_down.focus()
    expect(move_down).to_be_focused()
    page.page.keyboard.press("Enter")

    # The POST reordered and re-rendered: the second request lands on position 1.
    expect(first_key).to_have_value("extrato-receitas")
    expect(second_key).to_have_value("notas-fiscais")
    assert_policed_roles()  # the reorder POST ran as app_runtime too

    # Reorder is a DB fact, not a render trick: reload and read the same order.
    page.goto(live.url(f"/gestao/playbooks/{playbook.pk}/v/1/"))
    expect(first_key).to_have_value("extrato-receitas")

    axe_check(page.page)
    page.assert_clean_console()
    page.screenshot("playbook-editor")


def test_the_editor_axe_passes_on_a_flagged_firm(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    live = live_firm()
    playbook = _clone_pack(live)
    page = open_page(live, None)
    page.goto(live.url(f"/gestao/playbooks/{playbook.pk}/v/1/"))
    expect(page.page.get_by_role("button", name="Publicar versão")).to_be_visible()
    axe_check(page.page)
    page.assert_clean_console()
