"""Firm branding for the client portal: a name, one colour and a small PNG logo.

**Colour.** The portal draws the brand colour as the header fill and writes the firm's
name on it. The colour is validated against the two foregrounds the product owns --
white and the ink token (`--color-ink-900`, `oklch(0.19 0.017 250)` in
`assets/css/app.css`, which is sRGB `#0e141b`) -- and the header uses whichever of the
two reaches WCAG AA (4.5:1) against it, white first. A colour where NEITHER reaches
4.5:1 is refused before it is saved: there is no text colour this product could put on
it that a reader with low vision could rely on.

**Logo.** PNG only, at most 200 KB, recognised by its signature rather than by the
file name or the declared content type. SVG is refused outright, because an SVG is a
document that can carry script and external references, and the portal serves the
logo from its own origin. The bytes live in storage and reach a browser only through
the app-mediated portal route, never as a public storage URL.
"""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from django.core.exceptions import ValidationError

if TYPE_CHECKING:
    from django.core.files.base import File

HEX_COLOR: Final = re.compile(r"^#[0-9a-fA-F]{6}$")
AA_NORMAL_TEXT: Final = 4.5
WHITE_HEX: Final = "#ffffff"
# `--color-ink-900` from assets/css/app.css, converted from OKLCH to sRGB.
INK_HEX: Final = "#0e141b"
LOGO_MAX_BYTES: Final = 200 * 1024
PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
BRAND_NAME_MAX_LENGTH: Final = 80

# The linearisation constants of the sRGB transfer function (WCAG 2.x definition).
_SRGB_KNEE: Final = 0.04045
_SRGB_SLOPE: Final = 12.92
_SRGB_OFFSET: Final = 0.055
_SRGB_SCALE: Final = 1.055
_SRGB_GAMMA: Final = 2.4
_FLARE: Final = 0.05


def _channel(value: int) -> float:
    unit = value / 255
    if unit <= _SRGB_KNEE:
        return unit / _SRGB_SLOPE
    return float(((unit + _SRGB_OFFSET) / _SRGB_SCALE) ** _SRGB_GAMMA)


def relative_luminance(hex_color: str) -> float:
    """WCAG relative luminance of a `#rrggbb` colour."""
    red, green, blue = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)


def contrast_ratio(first: str, second: str) -> float:
    """WCAG contrast ratio between two `#rrggbb` colours, from 1 to 21."""
    lighter, darker = sorted(
        (relative_luminance(first), relative_luminance(second)), reverse=True
    )
    return (lighter + _FLARE) / (darker + _FLARE)


def foreground_for(hex_color: str) -> str | None:
    """Return the product foreground reaching AA on this fill, white first, or None."""
    for candidate in (WHITE_HEX, INK_HEX):
        if contrast_ratio(hex_color, candidate) >= AA_NORMAL_TEXT:
            return candidate
    return None


def validate_brand_color(value: str) -> None:
    """Refuse a malformed colour, or one no product foreground can be read on."""
    if not value:
        return
    if not HEX_COLOR.fullmatch(value):
        msg = "Informe a cor no formato #RRGGBB."
        raise ValidationError(msg, code="brand_color_format")
    if foreground_for(value) is None:
        white = contrast_ratio(value, WHITE_HEX)
        ink = contrast_ratio(value, INK_HEX)
        msg = (
            f"Esta cor não tem contraste suficiente nem com texto branco "
            f"({white:.1f}:1) nem com o texto escuro do produto ({ink:.1f}:1). "
            f"Escolha uma cor mais clara ou mais escura (mínimo 4,5:1)."
        )
        raise ValidationError(msg, code="brand_color_contrast")


def validate_logo(upload: "File[bytes]") -> None:
    """Refuse anything but a PNG of at most 200 KB, by size and by signature."""
    size = upload.size
    if size is None or size > LOGO_MAX_BYTES:
        msg = "O logotipo deve ter no máximo 200 KB."
        raise ValidationError(msg, code="logo_too_large")
    # A stored FieldFile opens lazily on read; close it again if this opened it.
    opened_here = upload.closed
    upload.seek(0)
    head = upload.read(len(PNG_SIGNATURE))
    upload.seek(0)
    if opened_here:
        upload.close()
    if head != PNG_SIGNATURE:
        msg = "Envie o logotipo em PNG. SVG e outros formatos não são aceitos."
        raise ValidationError(msg, code="logo_not_png")


def logo_upload_to(instance: object, _filename: str) -> str:
    """Store under a fixed, firm-keyed name: the client's filename is never used.

    The tenant's UUIDv7 primary key is assigned in Python before the first save.
    """
    return f"branding/{getattr(instance, 'pk', None)}/logo.png"


@dataclass(frozen=True, slots=True)
class Brand:
    """What the portal shell draws, resolved as `app_runtime` before the role switch.

    Scalars only: `app_portal` cannot read `tenants_tenant`, so the template must never
    be handed the row.
    """

    name: str
    color: str
    foreground: str
    logo_key: str


def brand_for(tenant: object) -> Brand:
    """Flatten a tenant row into the values the portal header renders."""
    color = str(getattr(tenant, "brand_color", "") or "")
    foreground = foreground_for(color) if color else None
    return Brand(
        name=str(getattr(tenant, "brand_name", "") or ""),
        color=color if foreground else "",
        foreground=foreground or "",
        logo_key=str(getattr(getattr(tenant, "logo", None), "name", "") or ""),
    )
