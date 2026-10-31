"""AXRM-017: staff obtain the official DAS guide and the publication form offers it."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from playwright.sync_api import expect

from apps.accounts.models import User
from tests.browser.harness import BrowserPage, LiveFirm, axe_check
from tests.fakes.serpro import FakeSerpro
from tests.integrations.pgmei_support import LazyModule, build_world, das_reply
from tests.support import enrol_totp
from tests.ui.factories import Firm

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from pytest_django.fixtures import SettingsWrapper
    from pytest_django.live_server_helper import LiveServer

    from apps.integrations import tasks
    from apps.integrations.serpro import pgmei
else:
    pgmei = LazyModule("apps.integrations.serpro.pgmei")
    tasks = LazyModule("apps.integrations.tasks")

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]


def test_staff_obtain_the_guide_and_select_it_for_publication(
    browser_server: LiveServer,
    open_page: OpenPage,
    settings: SettingsWrapper,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = build_world(settings, tmp_path)
    enrol_totp(world.owner)
    enrol_totp(world.staff)
    live = LiveFirm(
        Firm(
            tenant=world.termo.tenant,
            owner=world.owner,
            accountant=world.staff,
            clients=[world.termo.client],
        ),
        browser_server.thread.port,
    )
    with FakeSerpro((das_reply(),)) as peer:
        monkeypatch.setattr(pgmei, "fake_origin", lambda: peer.origin)
        # The worker is this process: the committed request runs the real task.
        monkeypatch.setattr(
            tasks.obtain_das_guide_task,
            "delay",
            tasks.obtain_das_guide_task,
        )
        staff = open_page(live, world.staff)
        staff.goto(live.url(f"/ciclos/{world.cycle.pk}/"))
        action = staff.page.get_by_role("button", name="Obter guia DAS oficial")
        expect(action).to_be_visible()
        staff.screenshot("axrm017-cycle-before-guide")
        axe_check(staff.page)
        with staff.page.expect_response(
            lambda response: (
                response.request.method == "POST" and "guia-das-oficial" in response.url
            )
        ) as posted:
            action.click()
        assert posted.value.status in {200, 302}
        guide = staff.page.locator("[data-das-guide]")
        expect(guide).to_contain_text(pgmei.GUIDE_TITLE)
        expect(guide).to_contain_text("Guia obtida")
        expect(
            staff.page.locator(f'[data-request-state="{pgmei.FIRM_ONLY_STATE}"]')
        ).to_contain_text("Interno do escritório")
        assert len(peer.requests) == 1
    assert staff.page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    staff.screenshot("axrm017-cycle-guide-linked")
    axe_check(staff.page)
    staff.goto(live.url(f"/ciclos/{world.cycle.pk}/propor/"))
    expect(
        staff.page.get_by_text(pgmei.GUIDE_FILENAME_PREFIX, exact=False).first
    ).to_be_visible()
    assert staff.page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    staff.screenshot("axrm017-publication-form-lists-guide")
    axe_check(staff.page)
    staff.assert_clean_console()
