"""Every control this project draws says what it is, on both surfaces at once.

WHAT THIS PROTECTS. `tests/ui/test_icons.py` pins each of the sixteen icon partials as
decorative: a single inline `<svg>` carrying `aria-hidden="true"` and
`focusable="false"`. That is the right call for a glyph sitting beside a word — an
assistive technology should read "Exportar clientes", not "Exportar clientes, download
icon" — and its own comment says the consequence out loud: "An icon-only control still
needs an accessible name of its own; that rule belongs where the control is built, not
here." This file is where that rule lives.

Because of `aria-hidden`, a `<button>` or `<a>` containing nothing but an icon include
has no accessible name at all. Not a poor one — none. A screen reader announces
"button" and stops. The person hearing it is told a control exists and is told nothing
whatsoever about what pressing it would do, and there is no recovery available to them:
they cannot see the glyph, and there is no text to fall back on. It is also completely
invisible to every other test in this project, because the page renders, the layout is
correct, the control works, and a sighted reviewer sees a perfectly good icon button.

THE SURFACE IS COMPLIANT TODAY, ON PURPOSE. Every one of the icon call sites this
project ships pairs its glyph with a word — `portal/partials/nav.html` states the
convention in its own note: "the label is never optional and the icon never stands
alone". So this file passes on the day it is written. That is what a regression guard
looks like before the regression, and it is the same shape as the CSRF leg in
`tests/ui/test_form_contract.py`: the value is not in today's answer, it is in the
first compact icon button somebody adds six months from now.

WHICH MAKES THE DETECTOR'S OWN PROOF THE LOAD-BEARING PART. An empty offender list is
what a compliant surface produces AND what a scan that stopped recognising icons
produces. `a11y_scan.unnamed_icon_controls` therefore runs a planted pair through its
own machinery before it reads a single real template: a synthetic icon-only button that
must be reported, and the same button plus a `.visually-hidden` label that must not be.
Both that gate and the floor on real controls live inside the helper, where `pytest -k`
cannot deselect them while the cases here keep running green over nothing.

ONE SCAN, BOTH SURFACES. Unlike the reflow contract, this one is not mirrored. The
reason the portal needs its own reflow twin is that the two SHELLS are different files
holding the same promise separately — but a control's accessible name is a property of
the control's own markup, not of the layout wrapping it, and `loaded_templates()`
already reaches every template both shells render. Splitting the walk would give two
floors to keep in step for no promise the single walk fails to hold. The partition case
at the bottom pins that the walk really does reach both.
"""

from pathlib import Path
from typing import Final

from django.conf import settings

from tests.ui.a11y_scan import (
    HIDDEN_LABEL,
    controls_carrying_icons,
    unnamed_icon_controls,
)
from tests.ui.templates_scan import loaded_templates

SURFACE: Final = "project"

# The floor for the non-vacuity gate. Eleven controls draw an icon inside themselves
# today — the firm shell's menu disclosure, the registry's export link, the invitation
# page's confirmation, the portal's four bar destinations, its download link, its two
# account rows and its upload button. Six is comfortably under that, so retiring a
# screen is not a failure, and comfortably above zero, so an include spelled a new way
# or an icon directory renamed cannot report itself as compliance.
ICON_CONTROL_FLOOR: Final = 6

PORTAL_ROOT: Final[Path] = Path(settings.BASE_DIR) / "apps" / "portal" / "templates"
FIRM_SHELL: Final[Path] = Path(settings.BASE_DIR) / "templates" / "base.html"


def test_no_control_draws_a_glyph_and_announces_nothing() -> None:
    """A control containing only an icon is a button with no name.

    Every icon partial is `aria-hidden="true"`, so the glyph contributes nothing to
    the accessible name by construction. A control that holds one and nothing else is
    therefore announced as its role alone — "button", "link" — which tells a reader
    that something is there and refuses to tell them what.

    Three remedies satisfy this and the scan accepts all three, because they are the
    same promise written three ways: visible text beside the glyph, hidden text in a
    `.visually-hidden` span, or an `aria-label` on the control. What is refused is a
    control that offers none of them.
    """
    offenders = unnamed_icon_controls(
        list(loaded_templates()),
        surface=SURFACE,
        minimum=ICON_CONTROL_FLOOR,
    )
    assert offenders == [], (
        f"these controls draw a decorative glyph and carry no name of any kind — no "
        f"text, no .{HIDDEN_LABEL} label and no aria-label — so each is announced as "
        f'its role and nothing more. Add <span class="{HIDDEN_LABEL}"> naming what the '
        f"control does: {offenders}"
    )


def test_the_walk_reaches_the_controls_on_both_shells() -> None:
    """The partition, pinned once, so the case above is about the whole product.

    `loaded_templates()` is meant to reach the firm's tree and the portal's app
    directory alike. If discovery ever narrowed to one of them, the case above would
    keep passing while saying nothing at all about the other — and the portal is the
    surface where an unlabelled glyph costs the most, because it is a phone in the hand
    of somebody who is not an accountant.
    """
    controls = controls_carrying_icons(
        list(loaded_templates()),
        surface=SURFACE,
        minimum=ICON_CONTROL_FLOOR,
    )
    roots = {control.path for control in controls}

    assert any(path.is_relative_to(PORTAL_ROOT) for path in roots), (
        f"no control drawing an icon was found under {PORTAL_ROOT.as_posix()}; the "
        f"walk is not reaching the portal, so the naming rule above is a claim about "
        f"the firm's screens only"
    )
    assert any(not path.is_relative_to(PORTAL_ROOT) for path in roots), (
        f"every control drawing an icon came from {PORTAL_ROOT.as_posix()}; the walk "
        f"is not reaching the firm's templates, so the naming rule above says nothing "
        f"about the shell in {FIRM_SHELL.as_posix()}"
    )
