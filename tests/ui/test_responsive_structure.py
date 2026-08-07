"""The reflow contract the firm's screens hold, scanned across every template.

WHAT THIS PROTECTS. An accountant opens this product on a laptop and a phone in the
same morning. Four structural promises are what make the second one usable, and all
four fail the same way — silently, on a device nobody develops against, on the day
somebody adds a column:

  1. A wide table either scrolls inside its own container or dissolves into labelled
     blocks. Neither, and it pushes the whole page sideways at 360.
  2. Nothing is pinned to a pixel count. A box that cannot shrink is a box that
     overflows, and one that cannot grow clips a reader who enlarged their type.
  3. The shell declares the viewport. Without it the document lays out at 980 CSS
     pixels and is scaled down — every figure too small to read, every control too
     small to hit — and every other promise here is then measured against a viewport
     that is not the device's.
  4. A grid that lays out three or more columns declares that only behind a
     breakpoint. Three columns of 360 minus the gutters is not a layout, it is four
     truncated words.

WHAT THIS IS NOT. It is not a snapshot of utility classes. `sm:grid-cols-2` is read
for the PROPERTY it carries — a column count that moves at a breakpoint — and never
compared against its own spelling, because a suite that reddens on a legitimate
restyle stops being read, and a suite nobody reads catches nothing at all. The only
literals here are the four the contracts are written in: `.data-table`, `.table-wrap`,
`.row-stack` and `data-label`.

THE REVIEW WIDTHS ARE 360 / 768 / 1024 / 1440 — `responsive_scan.REVIEW_WIDTHS`.
Nothing below measures a rendered pixel, because a Django test client has no viewport;
these are the widths the structure scanned here has to survive, and they are the same
four the optional screenshot pass reviews at. Named in one place so the two cannot
drift apart without somebody editing that tuple.

THE NON-VACUITY GATES LIVE IN THE HELPERS, not in sentinel cases of their own. Every
claim below is an assertion that some list is empty, and an empty list is exactly what
a scan that reached no files, a surface that lost its tables, or a regex edited into
uselessness all produce. A standalone sentinel is one `pytest -k` away from being
deselected while the cases depending on it keep running green over nothing; an assert
inside the helper each case calls cannot be deselected at all.

The portal holds the same four promises under its own standalone shell and is scanned
by `tests/portal/test_responsive_structure.py`. Mirrored rather than shared, for the
reason `tests/portal/test_portal_layout.py` gives: the two layouts are different files
by construction, so a scan that only ever walks this one says nothing about that one.
"""

from pathlib import Path
from typing import Final

from django.conf import settings

from tests.ui.responsive_scan import (
    COLUMN_LABEL,
    DATA_TABLE,
    DEVICE_WIDTH,
    NARROW_COLUMN_CEILING,
    REVIEW_WIDTHS,
    ROW_STACK,
    TABLE_WRAP,
    ZOOM_LOCKS,
    column_declarations,
    data_tables,
    firm_templates,
    fixed_pixel_widths,
    unlabelled_cells,
    unreflowed,
    viewport_of,
)

SURFACE: Final = "firm"

# The firm's shell, and the standalone entrance shell every allauth screen renders
# inside. The second one extends nothing — see its own note for why sharing the first
# would 500 the portal's sign-in page — so it holds the viewport promise separately or
# not at all, and "not at all" is a sign-in page scaled down to illegibility on the
# first screen anybody ever sees.
FIRM_SHELL: Final[Path] = Path(settings.BASE_DIR) / "templates" / "base.html"
ENTRANCE_SHELL: Final[Path] = (
    Path(settings.BASE_DIR) / "templates" / "allauth" / "layouts" / "base.html"
)

# The four grids this contract is named for: two metric ramps, the twelve-month DAS
# calendar, and the client identity facts. Named by template rather than by class
# string — the claim is that each of these surfaces moves its column count at a
# breakpoint, not that it does so with any particular utility.
RESPONSIVE_GRID_SURFACES: Final = (
    "obligations/_counts.html",
    "core/dashboard.html",
    "obligations/_calendar_strip.html",
    "clients/_identity.html",
)

# Floors for the non-vacuity gates, each well under what the surface ships today and
# each far above zero. Seven tables live across five templates, so five survives
# retiring a whole screen while still refusing a surface that lost the family. The
# other three are round numbers under 332 class attributes, 36 data cells and 11
# column declarations: enough headroom for ordinary churn, tight enough that a parse
# which quietly stopped working cannot report itself as compliance.
TABLE_FLOOR: Final = 5
CLASS_ATTRIBUTE_FLOOR: Final = 100
DATA_CELL_FLOOR: Final = 20
COLUMN_DECLARATION_FLOOR: Final = len(RESPONSIVE_GRID_SURFACES)


def test_every_data_table_scrolls_or_stacks() -> None:
    """A wide table gets its own scroll container, or it dissolves into blocks.

    The two are alternatives, not a pair: `.table-wrap` keeps five columns reachable
    by letting the table scroll inside a box the page's width still governs, and
    `.row-stack` turns each row into a labelled block below `sm`. Either keeps the
    document itself from being pushed sideways at 360, which is the property. A table
    with neither takes the page with it, and the columns past the fold become
    unreachable rather than merely cramped.
    """
    tables = data_tables(firm_templates(), surface=SURFACE, minimum=TABLE_FLOOR)
    offenders = unreflowed(tables)
    assert offenders == [], (
        f"these .{DATA_TABLE} elements sit in no .{TABLE_WRAP} and carry no "
        f".{ROW_STACK}, so at {REVIEW_WIDTHS[0]}px they push the page sideways "
        f"instead of reflowing: {offenders}"
    )


def test_every_stacked_cell_regrows_its_column_name() -> None:
    """`.row-stack` is only a treatment while the labels survive the stacking.

    Below `sm` the header row is hidden from sight and each cell reads its column name
    back out of `data-label`. A cell without one stacks into a bare figure with
    nothing saying whether it is a ceiling, an amount invoiced or a percentage
    consumed — which on this product's screens is the difference between a client who
    is fine and one who is about to be disenquadrada.
    """
    offenders = unlabelled_cells(
        firm_templates(),
        surface=SURFACE,
        minimum=DATA_CELL_FLOOR,
    )
    assert offenders == [], (
        f"these cells carry neither {COLUMN_LABEL} nor a colspan, so once a "
        f".{ROW_STACK} table stacks them at {REVIEW_WIDTHS[0]}px they are values with "
        f"no column name attached: {offenders}"
    )


def test_no_template_pins_a_box_to_a_pixel_count() -> None:
    """A width in pixels honours neither the viewport nor the reader's type size.

    `max-w-*` is deliberately outside this rule and is what the page measure is built
    from: a maximum still lets the box shrink, which is the whole property. What is
    refused is the literal pixel — as a Tailwind arbitrary value, as a presentational
    `width` attribute, or inside an inline style — because it is the one form that
    holds its size while the window narrows to 360 and while a reader turns their text
    up two steps.
    """
    offenders = fixed_pixel_widths(
        firm_templates(),
        surface=SURFACE,
        minimum=CLASS_ATTRIBUTE_FLOOR,
    )
    assert offenders == [], (
        f"these declarations pin a box to a pixel count, so it holds that width at "
        f"every one of {REVIEW_WIDTHS} and at every text size: {offenders}"
    )


def test_the_firm_shell_declares_a_reflowing_viewport() -> None:
    """Without the tag the layout is measured against a viewport nobody has."""
    tag = viewport_of(FIRM_SHELL, surface=SURFACE)
    assert DEVICE_WIDTH in tag, (
        f"{FIRM_SHELL.name} declares a viewport that is not the device's — {tag} — so "
        f"every page it wraps lays itself out at a desktop width and is then scaled "
        f"down, and none of the reflow above ever runs at {REVIEW_WIDTHS[0]}px"
    )
    locked = [lock for lock in ZOOM_LOCKS if lock in tag.replace(" ", "")]
    assert locked == [], (
        f"{FIRM_SHELL.name} takes pinch-zoom away from the reader — {locked} — which "
        f"is a WCAG 1.4.4 failure and the one recovery left to somebody whose eyes "
        f"the layout did not anticipate"
    )


def test_the_entrance_shell_declares_a_reflowing_viewport() -> None:
    """The allauth layout extends nothing, so it holds the promise on its own.

    It is also the shell rendered on BOTH hosts: `/accounts/` is mounted at the same
    path on the firm's subdomain and on the portal's, so this file is the sign-in page
    a MEI owner meets before any other screen in the product.
    """
    tag = viewport_of(ENTRANCE_SHELL, surface="entrance")
    assert DEVICE_WIDTH in tag, (
        f"the entrance shell declares a viewport that is not the device's — {tag} — "
        f"so the sign-in card is laid out wide and scaled down on the first screen a "
        f"client ever sees"
    )
    locked = [lock for lock in ZOOM_LOCKS if lock in tag.replace(" ", "")]
    assert locked == [], f"the entrance shell takes pinch-zoom away: {locked}"


def test_the_calendar_and_metric_grids_move_their_columns_at_a_breakpoint() -> None:
    """The four grids named in this contract each declare a column count behind `sm`.

    Asked as a property and never as a spelling. A grid satisfies this by declaring
    its columns behind any breakpoint at all; which breakpoint, and how many columns
    it opens up to, is a design decision that may be retuned between 768 and 1440
    without anybody having to come here first. What may not change silently is that
    the count moves — because a fixed one is a phone laying out a twelve-month
    calendar six across.
    """
    declared = column_declarations(
        firm_templates(),
        surface=SURFACE,
        minimum=COLUMN_DECLARATION_FLOOR,
    )
    responsive = {
        declaration.element.path.as_posix()
        for declaration in declared
        if declaration.breakpoint
    }
    missing = [
        surface
        for surface in RESPONSIVE_GRID_SURFACES
        if not any(path.endswith(surface) for path in responsive)
    ]
    assert missing == [], (
        f"these grids declare no column count behind any breakpoint, so they lay out "
        f"identically at {REVIEW_WIDTHS[0]}px and at {REVIEW_WIDTHS[-1]}px: {missing}"
    )


def test_no_grid_lays_out_three_columns_before_a_breakpoint() -> None:
    """The general form of the rule above, applied to every grid on the surface.

    Two columns unconditionally is a label beside its value, and it reflows: two
    halves of a narrow line still read. Three or more divides 360 minus the page
    gutters into columns too narrow to hold a CNPJ, a date or a currency figure
    without truncating it — so a count above the ceiling has to wait for a breakpoint
    that says the room is there.
    """
    offenders = [
        str(declaration)
        for declaration in column_declarations(
            firm_templates(),
            surface=SURFACE,
            minimum=COLUMN_DECLARATION_FLOOR,
        )
        if not declaration.breakpoint and declaration.count > NARROW_COLUMN_CEILING
    ]
    assert offenders == [], (
        f"these grids lay out more than {NARROW_COLUMN_CEILING} columns with no "
        f"breakpoint in front of the count, so they do it at {REVIEW_WIDTHS[0]}px "
        f"too: {offenders}"
    )


def test_the_firm_surface_is_the_one_being_scanned() -> None:
    """The partition itself, pinned once so every case above is about this surface.

    Without this, a change to how the two surfaces are told apart could hand every
    case here the portal's eight templates — or nothing at all — and each of them
    would keep passing while measuring the wrong thing entirely. The portal's own
    scan asserts the complement.
    """
    firm = firm_templates()
    assert len(firm) > len(RESPONSIVE_GRID_SURFACES), (
        f"the firm partition holds {len(firm)} templates, which is fewer than the "
        f"grids this contract names; the split between the two surfaces has drifted"
    )
    strays = [
        path.as_posix()
        for path, _ in firm
        if path.is_relative_to(Path(settings.BASE_DIR) / "apps")
    ]
    assert strays == [], (
        f"these portal-side templates reached the firm partition, so the two shells "
        f"are being scanned as one and the portal twin is no longer a twin: {strays}"
    )
    assert FIRM_SHELL in [path for path, _ in firm], (
        f"{FIRM_SHELL.as_posix()} is not among the templates this surface walks, so "
        f"the shell every case above reasons about is not the one being scanned"
    )
