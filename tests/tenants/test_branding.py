"""Firm branding: AA-validated colour, PNG-only small logo, app-mediated serving."""

from http import HTTPStatus
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from pytest_django.fixtures import SettingsWrapper

from apps.tenants.branding import (
    INK_HEX,
    LOGO_MAX_BYTES,
    PNG_SIGNATURE,
    WHITE_HEX,
    brand_for,
    contrast_ratio,
    foreground_for,
    validate_brand_color,
    validate_logo,
)
from tests.portal.request_fixtures import PortalSetup, portal_setup

PNG = PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + b"\x00" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def test_contrast_ratio_matches_the_wcag_reference_values() -> None:
    assert round(contrast_ratio("#000000", "#ffffff"), 2) == 21.0
    assert round(contrast_ratio("#777777", "#ffffff"), 2) == 4.48
    assert round(contrast_ratio("#ffffff", "#ffffff"), 2) == 1.0


@pytest.mark.parametrize(
    ("color", "foreground"),
    [
        ("#0f5b66", WHITE_HEX),  # deep teal: white text reaches AA
        ("#1e3a8a", WHITE_HEX),  # navy
        ("#fde68a", INK_HEX),  # pale yellow: only the ink token reaches AA
        ("#22d3ee", INK_HEX),
    ],
)
def test_a_readable_colour_is_accepted_with_the_foreground_that_passes(
    color: str, foreground: str
) -> None:
    validate_brand_color(color)
    assert foreground_for(color) == foreground
    assert contrast_ratio(color, foreground) >= 4.5


@pytest.mark.parametrize("color", ["#7a7a7a", "#2f80a0", "#4a8a5a", "#787878"])
def test_a_colour_neither_white_nor_ink_can_be_read_on_is_refused(color: str) -> None:
    assert contrast_ratio(color, WHITE_HEX) < 4.5
    assert contrast_ratio(color, INK_HEX) < 4.5
    with pytest.raises(ValidationError) as refused:
        validate_brand_color(color)
    assert refused.value.code == "brand_color_contrast"


@pytest.mark.parametrize("color", ["red", "#fff", "#12345g", "1e3a8a", "#1e3a8a;"])
def test_a_malformed_colour_is_refused(color: str) -> None:
    with pytest.raises(ValidationError) as refused:
        validate_brand_color(color)
    assert refused.value.code == "brand_color_format"


def test_a_png_up_to_200_kb_is_accepted() -> None:
    validate_logo(SimpleUploadedFile("logo.png", PNG, content_type="image/png"))
    exact = PNG + b"\x00" * (LOGO_MAX_BYTES - len(PNG))
    validate_logo(SimpleUploadedFile("logo.png", exact, content_type="image/png"))


@pytest.mark.parametrize(
    ("name", "payload", "code"),
    [
        ("logo.svg", SVG, "logo_not_png"),
        ("logo.png", SVG, "logo_not_png"),  # an SVG named .png is still refused
        ("logo.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 32, "logo_not_png"),
        ("logo.png", PNG + b"\x00" * LOGO_MAX_BYTES, "logo_too_large"),
    ],
)
def test_svg_other_formats_and_oversized_logos_are_refused(
    name: str, payload: bytes, code: str
) -> None:
    with pytest.raises(ValidationError) as refused:
        validate_logo(SimpleUploadedFile(name, payload, content_type="image/png"))
    assert refused.value.code == code


@pytest.fixture
def branded(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    settings.MEDIA_ROOT = tmp_path
    setup = portal_setup("branded")
    tenant = setup.graph.tenant
    tenant.brand_name = "Contábil Aurora"
    tenant.brand_color = "#0f5b66"
    tenant.logo.save("qualquer-nome.svg", ContentFile(PNG), save=False)
    tenant.full_clean()
    tenant.save()
    return setup


@pytest.mark.django_db(transaction=True)
def test_the_portal_shell_renders_the_brand_and_serves_it_through_the_app(
    branded: PortalSetup,
) -> None:
    tenant = branded.graph.tenant
    # The client's filename is never used for the stored object.
    assert tenant.logo.name == f"branding/{tenant.pk}/logo.png"
    browser = branded.portal()
    body = browser.get("/").content.decode()
    assert "Contábil Aurora" in body
    assert 'href="/marca/tema.css"' in body
    assert 'src="/marca/logo.png"' in body
    assert "style=" not in body
    css = browser.get("/marca/tema.css")
    assert css.status_code == HTTPStatus.OK
    assert css["Content-Type"].startswith("text/css")
    assert b"background-color:#0f5b66;color:#ffffff" in css.content
    logo = browser.get("/marca/logo.png")
    assert logo.status_code == HTTPStatus.OK
    assert logo["Content-Type"] == "image/png"
    assert logo.content == PNG
    # App-mediated: an anonymous request gets no logo.
    anonymous = type(browser)(SERVER_NAME=branded.portal_host)
    assert anonymous.get("/marca/logo.png").status_code == HTTPStatus.FOUND


@pytest.mark.django_db(transaction=True)
def test_an_unflagged_firm_shows_no_brand(branded: PortalSetup) -> None:
    tenant = branded.graph.tenant
    tenant.features = []
    tenant.save(update_fields=["features"])
    body = branded.portal().get("/").content.decode()
    assert "Contábil Aurora" not in body
    assert "/marca/" not in body


def test_brand_for_drops_a_stored_colour_that_fails_validation() -> None:
    class Row:
        brand_name = "X"
        brand_color = "#7a7a7a"
        logo = None

    brand = brand_for(Row())
    assert brand.color == ""
    assert brand.foreground == ""
