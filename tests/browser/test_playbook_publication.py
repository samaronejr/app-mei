"""Save, preview and publish the rendered editor with its unused extra row."""

from collections.abc import Callable
from http import HTTPStatus

import pytest
from playwright.sync_api import expect

from apps.accounts.models import User
from apps.audit.models import AuditAction, Event
from apps.core.tenancy import tenant_context
from apps.cycles.models import Playbook, VersionState
from apps.cycles.services import playbooks
from tests.browser.harness import (
    BrowserPage,
    LiveFirm,
    assert_policed_roles,
    axe_check,
)

pytestmark = pytest.mark.browser

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]


def test_save_preview_publish_keeps_the_untouched_extra_row_empty(
    live_firm: Callable[..., LiveFirm], open_page: OpenPage
) -> None:
    live = live_firm()
    with tenant_context(live.firm.tenant.pk):
        source = Playbook.objects.get(key="mei-mensal")
        playbook = playbooks.clone_playbook(
            actor=live.firm.owner,
            tenant=live.firm.tenant,
            playbook_pk=source.pk,
        )
        draft = playbook.versions.get()
        original_steps = list(
            draft.steps.order_by("position").values_list("key", flat=True)
        )
    browser = open_page(live, None)
    page = browser.page
    url = live.url(f"/gestao/playbooks/{playbook.pk}/v/1/")
    browser.goto(url)
    extra_index = len(original_steps)
    extra_key = page.locator(f"input[name='steps-{extra_index}-key']")
    extra_delete = page.locator(f"input[name='steps-{extra_index}-DELETE']")
    expect(extra_key).to_have_value("")
    expect(extra_delete).not_to_be_checked()
    expect(page.locator("input[name='steps-TOTAL_FORMS']")).to_have_value(
        str(extra_index + 1)
    )

    with page.expect_response(
        lambda response: response.url == url and response.request.method == "POST",
        timeout=10000,
    ) as saved:
        page.get_by_role("button", name="Salvar rascunho", exact=True).click()
    assert saved.value.status == HTTPStatus.FOUND
    expect(page.locator("#steps-form input[name='expected_version']")).to_have_value(
        "2"
    )
    expect(extra_key).to_have_value("")
    expect(extra_delete).not_to_be_checked()
    browser.screenshot("playbook-saved")

    with page.expect_response(
        lambda response: response.url == url and response.request.method == "POST",
        timeout=10000,
    ) as previewed:
        page.get_by_role("button", name="Pré-visualizar publicação", exact=True).click()
    assert previewed.value.status == HTTPStatus.OK
    expect(page.locator("#steps-form input[name='expected_version']")).to_have_value(
        "3"
    )
    expect(page.locator("input[name='preview_token']")).to_have_count(1)
    expect(extra_key).to_have_value("")
    expect(extra_delete).not_to_be_checked()
    axe_check(page)
    browser.screenshot("playbook-preview")

    with page.expect_response(
        lambda response: response.url == url and response.request.method == "POST",
        timeout=10000,
    ) as published:
        page.get_by_role(
            "button", name="Publicar esta pré-visualização", exact=True
        ).click()
    assert published.value.status == HTTPStatus.FOUND
    expect(page).to_have_url(live.url(f"/gestao/playbooks/{playbook.pk}/"))
    assert_policed_roles()
    browser.assert_clean_console()
    browser.screenshot("playbook-published")
    with tenant_context(live.firm.tenant.pk):
        draft.refresh_from_db()
        assert draft.state == VersionState.PUBLISHED
        assert draft.version == 4
        assert (
            list(draft.steps.order_by("position").values_list("key", flat=True))
            == original_steps
        )
        assert (
            Event.objects.filter(
                action=AuditAction.PLAYBOOK_PUBLISHED, object_id=str(draft.pk)
            ).count()
            == 1
        )
