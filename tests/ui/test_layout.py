"""The accessible shell every page inherits: landmarks, skip link, single heading."""

import re
from http import HTTPStatus

import pytest
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.core.templatetags.ptbr import BRT
from apps.tenants.models import Invite, TenantRole
from tests.ui.factories import Firm, make_firm
from tests.ui.templates_scan import loaded_templates

pytestmark = pytest.mark.django_db(transaction=True)

H1 = re.compile(r"<h1[\s>]", re.IGNORECASE)
LANDMARKS = ("<header", "<nav", "<main", "<footer")


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


@pytest.fixture
def firm() -> Firm:
    return make_firm("alpha-layout")


def _public_page() -> str:
    return Client(SERVER_NAME="localhost").get(reverse("dsr-submit")).content.decode()


def test_the_document_declares_brazilian_portuguese() -> None:
    assert '<html lang="pt-br">' in _public_page()


def test_every_page_carries_exactly_one_h1(firm: Firm) -> None:
    """Two <h1> elements make a screen reader's document outline ambiguous."""
    pages = [_public_page()]
    signed_in = firm.as_owner()
    for name in ("team", "invite-issued"):
        response = signed_in.get(reverse(name))
        assert response.status_code == HTTPStatus.OK, name
        pages.append(response.content.decode())

    for body in pages:
        assert len(H1.findall(body)) == 1


def test_no_template_but_the_layout_emits_an_h1() -> None:
    """The rule above holds structurally only while base.html owns the tag."""
    offenders = [
        str(path)
        for path, text in loaded_templates()
        if H1.search(text) and path.name != "base.html"
    ]
    assert offenders == []


def test_the_skip_link_is_the_first_focusable_element_and_has_a_target() -> None:
    body = _public_page()
    skip = re.search(r'<a class="skip-link" href="#([\w-]+)"', body)
    assert skip is not None, "no skip link"
    target = skip.group(1)

    # It must precede the header, or it is not the first thing a keyboard reaches.
    assert body.index("skip-link") < body.index("<header")
    # …and it must land on something focusable, or focus stays in the header.
    assert f'<main id="{target}" tabindex="-1"' in body


def test_the_page_exposes_the_four_landmarks(firm: Firm) -> None:
    body = firm.as_owner().get(reverse("team")).content.decode()
    for landmark in LANDMARKS:
        assert landmark in body, landmark


def test_a_nav_landmark_is_omitted_rather_than_left_empty() -> None:
    """An anonymous page draws no nav, and an empty landmark is noise to announce."""
    body = _public_page()
    assert "<main" in body
    assert "<nav" not in body


def test_the_focus_ring_is_defined_and_never_removed(settings: SettingsWrapper) -> None:
    """`outline: none` with no replacement is a WCAG 2.4.7 failure."""
    stylesheet = (settings.BASE_DIR / "static" / "css" / "app.css").read_text()
    compact = stylesheet.replace(" ", "")
    assert ":focus-visible" in compact
    assert "outline:2pxsolid" in compact


def test_a_timestamp_renders_in_brasilia_time_on_a_real_page(firm: Firm) -> None:
    """The filters are unit-tested; this proves the layout actually applies them."""
    invite, _token = Invite.issue(
        tenant=firm.tenant,
        email="convidada@example.com",
        role=TenantRole.STAFF_ACCOUNTANT,
    )
    body = firm.as_owner().get(reverse("team")).content.decode()
    expected = invite.expires_at.astimezone(BRT).strftime("%d/%m/%Y %H:%M")
    assert expected in body
