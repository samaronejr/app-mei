"""The reflow contract the portal's screens hold, under its own standalone shell.

A mirror of `tests/ui/test_responsive_structure.py`, walking the eight templates that
render inside `apps/portal/templates/portal/base.html`. Mirrored rather than shared,
for the reason `tests/portal/test_portal_layout.py` gives about the layouts: the two
shells are separate files by construction — the firm's calls `{% nav_items %}`
unconditionally and `app_portal` holds SELECT on none of the tables that reaches — and
that separation is exactly why every structural promise has to be pinned twice. A scan
that only ever walks the firm's templates says nothing about these, and each promise
would be rediscovered by a MEI owner on a phone rather than by a runner.

THE PORTAL ANSWERS CONTRACT 1 DIFFERENTLY, AND MORE STRONGLY. The firm's screens keep
a wide table usable by wrapping it in a scroll container or stacking its rows. The
portal ships no table at all: `apps/portal/templates/portal/payments.html` says why in
its own note — a five-column table on a phone is either a horizontal scroll or four
truncated columns, so every repeating record here is a card that stacks by
construction. Structural absence is the stronger form of the same promise, so this
file asserts the absence rather than reusing the firm's wrapping rule.

WHICH MAKES THE GATE THE INTERESTING PART. "This surface ships no table" is trivially
true of a surface that ships no records either, and would stay green straight through
a page that had quietly stopped rendering its list. So the absence is asserted only
after the walk has found the cards that replaced the tables, and that floor lives
inside `cards_in_lists` where `pytest -k` cannot deselect it.

THE REVIEW WIDTHS ARE 360 / 768 / 1024 / 1440 — `responsive_scan.REVIEW_WIDTHS`, the
same tuple the firm's scan reads and the same four the optional screenshot pass
reviews at. Nothing here measures a rendered pixel; these are the widths the structure
below has to survive. 360 is the one that matters most on this surface: it is a phone
in a hand, which is the only device a large share of these readers will ever open the
product on.

NO EXACT CLASS STRING IS PINNED beyond the four the contracts are written in. A column
count is read for whether it moves at a breakpoint, never for how it is spelled.
"""

from pathlib import Path
from typing import Final

from django.conf import settings

from tests.ui.responsive_scan import (
    DEVICE_WIDTH,
    NARROW_COLUMN_CEILING,
    REVIEW_WIDTHS,
    ZOOM_LOCKS,
    cards_in_lists,
    column_declarations,
    elements_of,
    fixed_pixel_widths,
    portal_templates,
    viewport_of,
)

SURFACE: Final = "portal"

PORTAL_SHELL: Final[Path] = (
    Path(settings.BASE_DIR) / "apps" / "portal" / "templates" / "portal" / "base.html"
)

# Floors for the non-vacuity gates, each well under what this surface ships today and
# each far above zero: four record cards, eighty-nine class attributes and four column
# declarations across eight templates. Enough headroom that retiring a screen is not a
# failure, tight enough that a parse which quietly stopped working cannot report
# itself as compliance.
RECORD_CARD_FLOOR: Final = 2
CLASS_ATTRIBUTE_FLOOR: Final = 25
COLUMN_DECLARATION_FLOOR: Final = 2

# Every element that would put a record in a row of cells rather than in a block.
TABULAR_ELEMENTS: Final = ("table", "td", "th")


def _tabular_markup() -> list[str]:
    """Return every tabular element written anywhere under the portal shell."""
    return [
        str(element)
        for path, text in portal_templates()
        for element, _ in elements_of(path, text)
        if element.name in TABULAR_ELEMENTS
    ]


def test_the_portal_stacks_its_records_instead_of_tabulating_them() -> None:
    """No table, no cell — every record is a card, and a card stacks by construction.

    The card list is read FIRST and is the gate. Without it this case would pass for a
    portal that had stopped rendering payments and documents altogether, which is the
    one failure a client would notice immediately and this suite would not.
    """
    templates = portal_templates()
    cards = cards_in_lists(templates, surface=SURFACE, minimum=RECORD_CARD_FLOOR)
    tabular = _tabular_markup()
    assert tabular == [], (
        f"the portal draws {len(cards)} record cards and also this tabular markup; a "
        f"row of cells on a {REVIEW_WIDTHS[0]}px screen is either a sideways scroll "
        f"or columns truncated to nothing, which is why every record here is a card: "
        f"{tabular}"
    )


def test_no_portal_template_pins_a_box_to_a_pixel_count() -> None:
    """The same rule the firm's screens hold, on the surface that needs it most.

    `max-w-*` stays outside it and is what the portal's own measure is built from: a
    maximum still lets the box shrink. What is refused is the literal pixel — a
    Tailwind arbitrary value, a presentational `width` attribute, an inline style —
    because it holds its size while the window narrows to 360 and while a reader turns
    their text up, and a MEI owner reading a due date is exactly the reader who does.
    """
    offenders = fixed_pixel_widths(
        portal_templates(),
        surface=SURFACE,
        minimum=CLASS_ATTRIBUTE_FLOOR,
    )
    assert offenders == [], (
        f"these portal declarations pin a box to a pixel count, so it holds that "
        f"width at every one of {REVIEW_WIDTHS} and at every text size: {offenders}"
    )


def test_the_portal_shell_declares_a_reflowing_viewport() -> None:
    """The portal's layout extends nothing, so it carries the tag itself or not at all.

    Not at all is the worst version of this failure in the whole product: the portal
    is the surface reached on a phone by default, and a document laid out at 980 CSS
    pixels and scaled down puts a payment deadline in type too small to read on the
    screen it was written for.
    """
    tag = viewport_of(PORTAL_SHELL, surface=SURFACE)
    assert DEVICE_WIDTH in tag, (
        f"the portal shell declares a viewport that is not the device's — {tag} — so "
        f"every page it wraps lays out wide and is scaled down, and none of the "
        f"stacking above ever runs at {REVIEW_WIDTHS[0]}px"
    )
    locked = [lock for lock in ZOOM_LOCKS if lock in tag.replace(" ", "")]
    assert locked == [], (
        f"the portal shell takes pinch-zoom away from the reader — {locked} — a WCAG "
        f"1.4.4 failure, and the last recovery available to somebody whose eyes this "
        f"layout did not anticipate"
    )


def test_no_portal_grid_lays_out_three_columns_before_a_breakpoint() -> None:
    """Two columns unconditionally is a label beside its value, and it reflows.

    That pairing is what every card on this surface uses: `Competência` next to the
    month, `Vencimento` next to the date. Two halves of a narrow line still read at
    360. Three or more divides the card's inner width into columns too narrow for a
    CNPJ or a currency figure, so a count above the ceiling has to wait for a
    breakpoint that says the room exists.
    """
    offenders = [
        str(declaration)
        for declaration in column_declarations(
            portal_templates(),
            surface=SURFACE,
            minimum=COLUMN_DECLARATION_FLOOR,
        )
        if not declaration.breakpoint and declaration.count > NARROW_COLUMN_CEILING
    ]
    assert offenders == [], (
        f"these portal grids lay out more than {NARROW_COLUMN_CEILING} columns with "
        f"no breakpoint in front of the count, so they do it at {REVIEW_WIDTHS[0]}px "
        f"too: {offenders}"
    )


def test_the_portal_surface_is_the_one_being_scanned() -> None:
    """The partition itself, pinned once so every case above is about this surface.

    The complement of the firm scan's own partition case. Without both, a change to
    how the two surfaces are told apart could hand this file the firm's sixty-three
    templates — or nothing at all — and every case here would keep passing while
    measuring markup rendered under a shell it says nothing about.
    """
    templates = portal_templates()
    root = PORTAL_SHELL.parent.parent
    strays = [path.as_posix() for path, _ in templates if not path.is_relative_to(root)]
    assert strays == [], (
        f"these templates reached the portal partition from outside {root.as_posix()}"
        f", so this file is scanning markup the portal shell never wraps: {strays}"
    )
    assert PORTAL_SHELL in [path for path, _ in templates], (
        f"{PORTAL_SHELL.as_posix()} is not among the templates this surface walks, so "
        f"the shell every case above reasons about is not the one being scanned"
    )
