"""Cycle display cuts must not replace live scope or navigation authorization."""

from collections.abc import Iterable
from html.parser import HTMLParser
from typing import TYPE_CHECKING, override
from uuid import UUID

import pytest
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.authz.models import GrantLevel, Role, RoleGrant
from apps.authz.services import granted_levels
from apps.clients.models import ClientAssignment
from apps.core.tenancy import tenant_context
from apps.cycles.capabilities import CYCLES_OPERATE, CYCLES_REVIEW
from apps.cycles.models import InternalNote, ServiceCycle
from tests.cycles.display_fixtures import populated_detail
from tests.portal.request_fixtures import portal_setup

if TYPE_CHECKING:
    from apps.authz.services import Actor

pytestmark = pytest.mark.django_db(transaction=True)


class NavigationLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_navigation = False
        self.hrefs: set[str] = set()

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "nav":
            self.in_navigation = True
        href = dict(attrs).get("href")
        if self.in_navigation and tag == "a" and href:
            self.hrefs.add(href)

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag == "nav":
            self.in_navigation = False


def test_navigation_resolves_after_the_cycle_display_capability_cut(
    settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a cycle display read followed by a live matrix change before navigation.
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    world = populated_detail("t36-live-navigation")
    browser = world.firm()
    path = f"/ciclos/{world.cycle.pk}/"
    before = browser.get(path)
    links = NavigationLinks()
    links.feed(before.content.decode())
    destination = reverse("document-queue")
    assert before.status_code == 200
    assert destination in links.hrefs
    calls: list[dict[str, str]] = []

    def resolve_display_then_revoke(
        user: "Actor", actions: Iterable[str], *, tenant_id: UUID | None = None
    ) -> dict[str, str]:
        levels = granted_levels(user, actions, tenant_id=tenant_id)
        calls.append(levels)
        RoleGrant.objects.filter(
            role=Role.OWNER, capability__slug=CYCLES_REVIEW
        ).update(level=GrantLevel.NONE)
        return levels

    monkeypatch.setattr(
        "apps.cycles.display_reads.granted_levels", resolve_display_then_revoke
    )
    # When the GET renders its independently authorized navigation.
    response = browser.get(path)
    # Then the display cut was FULL, but navigation reads the later denial.
    links = NavigationLinks()
    links.feed(response.content.decode())
    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0][CYCLES_REVIEW] == GrantLevel.FULL
    assert reverse("clients-list") in links.hrefs
    assert destination not in links.hrefs


def test_display_rechecks_revoked_assignment_and_foreign_cycle(
    settings: SettingsWrapper,
) -> None:
    # Given an assigned actor and an independently populated foreign tenant.
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    world = populated_detail("t36-live-scope")
    foreign = portal_setup("t36-foreign-scope")
    browser = world.firm(world.graph.staff)
    path = f"/ciclos/{world.cycle.pk}/"
    positive = browser.get(path)
    assert positive.status_code == 200
    assert world.graph.client.legal_name.encode() in positive.content
    with tenant_context(world.graph.tenant.pk):
        ClientAssignment.objects.filter(
            client=world.graph.client, user=world.graph.staff
        ).delete()
    # When the same session reloads after revocation, or uses a foreign cycle id.
    revoked = browser.get(path)
    other = world.firm().get(f"/ciclos/{foreign.cycle.pk}/")
    # Then neither read confirms private cycle material.
    assert revoked.status_code == 404
    assert other.status_code == 404
    assert b"PRIVATE-T36-DISPLAY-NOTE" not in revoked.content
    assert foreign.graph.client.legal_name.encode() not in other.content


def test_live_command_denial_is_not_replaced_by_an_earlier_display_cut(
    settings: SettingsWrapper,
) -> None:
    # Given a successful display and a later live loss of operation authority.
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    world = populated_detail("t36-live-command")
    browser = world.firm()
    path = f"/ciclos/{world.cycle.pk}/"
    displayed = browser.get(path)
    assert displayed.status_code == 200
    assert displayed.context["may_operate"]
    version = displayed.context["cycle"].version
    with tenant_context(world.graph.tenant.pk):
        notes = InternalNote.objects.filter(cycle=world.cycle).count()
        RoleGrant.objects.filter(
            role=Role.OWNER, capability__slug=CYCLES_OPERATE
        ).update(level=GrantLevel.NONE)
    # When the same session posts the version it was actually shown.
    refused = browser.post(
        path,
        {
            "action": "note",
            "expected_version": version,
            "reason": "This display must not authorize a later write.",
        },
    )
    # Then the command's live decision refuses it without writing a note/version.
    assert refused.status_code == 403
    with tenant_context(world.graph.tenant.pk):
        assert InternalNote.objects.filter(cycle=world.cycle).count() == notes
        assert ServiceCycle.objects.get(pk=world.cycle.pk).version == version
