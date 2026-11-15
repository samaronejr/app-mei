"""Captured originals and summaries are reviewed in the existing firm surfaces."""

from collections.abc import Callable
from pathlib import Path

import pytest
from django.urls import reverse
from playwright.sync_api import expect
from pytest_django.fixtures import SettingsWrapper
from pytest_django.live_server_helper import LiveServer

from apps.accounts.models import User
from tests.browser.harness import BrowserPage, LiveFirm, axe_check
from tests.integrations.capture_fixtures import capture_world
from tests.obligations.fiscal_fixtures import summary
from tests.ui.factories import Firm

pytestmark = pytest.mark.browser


def _visible_snapshot(opened: BrowserPage, name: str) -> None:
    opened.screenshot(name)
    assert opened.page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    axe_check(opened.page)
    opened.assert_clean_console()


def test_captured_queue_review_cycle_and_hoje_at_both_widths(
    browser_server: LiveServer,
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
    settings: SettingsWrapper,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = capture_world(settings, tmp_path, monkeypatch)
    tenant = world.setup.graph.tenant
    tenant.features.append("briefing_r1")
    tenant.save(update_fields=["features"])
    original = world.ingest()
    incomplete = world.ingest(summary=summary())
    live = LiveFirm(
        firm=Firm(
            tenant=tenant,
            owner=world.setup.graph.actor,
            accountant=world.setup.graph.staff,
            clients=[world.link.client],
        ),
        port=browser_server.thread.port,
    )
    opened = open_page(live, None)
    page = opened.page
    opened.goto(live.url("/?escopo=team&periodo=2026-09"))
    _visible_snapshot(opened, "AXRM-021-hoje")
    expect(
        page.get_by_text("Documento capturado aguardando revisão", exact=True)
    ).to_have_count(2)

    queue = live.url(reverse("document-queue") + "?fila=fiscais")
    opened.goto(queue)
    expect(page.locator("[data-fiscal-document]")).to_have_count(2)
    expect(page.locator(f'[data-fiscal-document="{original.pk}"]')).to_contain_text(
        "Capturado do provedor"
    )
    _visible_snapshot(opened, "AXRM-021-queue")

    opened.goto(live.url(reverse("document-review", args=[incomplete.pk])))
    expect(page.locator("[data-fiscal-review]")).to_be_visible()
    expect(page.locator('#fiscal-decision option[value="aceito"]')).to_be_disabled()
    _visible_snapshot(opened, "AXRM-021-summary-review")
    page.get_by_label("Decisão sobre esta versão").select_option("requer_xml")
    page.get_by_role("button", name="Registrar revisão").click()
    expect(page).to_have_url(queue)

    opened.goto(live.url(reverse("document-review", args=[original.pk])))
    expect(page.get_by_role("link", name="XML original")).to_be_visible()
    page.get_by_label("Decisão sobre esta versão").select_option("aceito")
    page.get_by_label("Nota interna").fill("Revisao sintetica dos bytes retidos.")
    _visible_snapshot(opened, "AXRM-021-full-review")
    page.get_by_role("button", name="Registrar revisão").click()
    expect(page).to_have_url(queue)
    expect(page.locator("[data-fiscal-document]")).to_have_count(0)

    opened.goto(live.url(reverse("cycle-detail", args=[world.setup.cycle.pk])))
    panel = page.locator("[data-fiscal-documents]")
    expect(panel).to_be_visible()
    expect(panel.locator("[data-fiscal-document]")).to_have_count(2)
    expect(panel).to_contain_text("Aceito pela equipe")
    expect(panel).to_contain_text("Revisão: requer XML")
    panel.scroll_into_view_if_needed()
    _visible_snapshot(opened, "AXRM-021-cycle")


def test_flag_off_keeps_the_existing_cycle_usable(
    browser_server: LiveServer,
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
    settings: SettingsWrapper,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = capture_world(settings, tmp_path, monkeypatch)
    world.ingest()
    tenant = world.setup.graph.tenant
    tenant.features = ["cycles_r1", "scan_required"]
    tenant.save(update_fields=["features"])
    live = LiveFirm(
        firm=Firm(
            tenant=tenant,
            owner=world.setup.graph.actor,
            accountant=world.setup.graph.staff,
            clients=[world.link.client],
        ),
        port=browser_server.thread.port,
    )
    opened = open_page(live, None)
    opened.goto(live.url(reverse("cycle-detail", args=[world.setup.cycle.pk])))
    expect(opened.page.locator("[data-fiscal-documents]")).to_have_count(0)
    expect(
        opened.page.get_by_role("heading", name="Solicitações ao cliente", exact=True)
    ).to_be_visible()
    _visible_snapshot(opened, "AXRM-021-cycle-flag-off")
