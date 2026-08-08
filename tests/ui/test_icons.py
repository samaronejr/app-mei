"""Every icon partial is a self-contained, decorative, same-origin inline `<svg>`.

Icons are hand-copied Lucide glyphs committed as templates rather than pulled from a
package or a CDN, because an inline `<svg>` needs no CSP allowance at all: it is
markup the parser has already consumed, not a sub-resource the browser goes and
fetches. `apps/security/csp.py` is `script-src 'self'` with no external origin
anywhere in it, and `tests/ui/test_csp.py` reds the moment a template names one — so
the cheapest way to keep the policy this strict is for the glyphs to arrive as bytes.

They live in the *pure* partial bucket because the RLS-isolated client portal reuses
them, and a portal template may neither resolve a capability nor reach a firm-only
table while rendering (see `tests/portal/test_portal_templates_render.py`).

`aria-hidden="true"` plus `focusable="false"` make every glyph decorative by default,
so a reader never announces "image" beside a label that already says the word, and a
keyboard user never lands on a shape. An icon-only control still needs an accessible
name of its own; that rule belongs where the control is built, not here.

Every rule below quantifies over a glob, and `templates/partials/pure/icons/` does not
exist until the glyphs are vendored. So `_icon_partials` refuses to hand back a set
that proves nothing, and it is the only way in: an empty scan must fail *there*, never
pass vacuously in all seven rules beneath it. A dedicated sentinel test would not be
enough on its own -- `pytest -k` on any single rule does not select it, and the rule
then reports success over nothing.
"""

import re
from pathlib import Path
from typing import Final

from django.conf import settings

from tests.ui.templates_scan import attributes_of

ICONS: Final[Path] = (
    Path(settings.BASE_DIR) / "templates" / "partials" / "pure" / "icons"
)

# The glyphs this interface is built from. Additive by construction: a later wave adds
# a name here and drops the partial beside it, and every structural rule below covers
# the new file without one of them being edited.
EXPECTED_ICONS: Final[frozenset[str]] = frozenset(
    {
        "alert-triangle",
        "building-2",
        "calendar-clock",
        "check-circle",
        "chevron-right",
        "download",
        "file-text",
        "folder",
        "home",
        "info",
        "menu",
        "upload",
        "user-round",
        "users",
        "wallet",
        "x-circle",
    },
)

SVG_OPEN: Final = re.compile(r"<svg\b", re.IGNORECASE)
ROOT_TAG: Final = re.compile(r"<svg\b[^>]*>", re.IGNORECASE | re.DOTALL)
SCRIPT: Final = re.compile(r"<script\b", re.IGNORECASE)

# `data-label` does not match: the colon has to be the very next character.
DATA_URI: Final = re.compile(r"\bdata:", re.IGNORECASE)

# A namespace declaration is an identifier the browser never dereferences, and Lucide's
# copy-paste output carries one. Removing it first is what lets the origin scan be
# absolute rather than a list of allowed hosts: any `//` left in an icon is a URL, and
# that single rule catches `http://`, `https://` and protocol-relative `//host` alike.
NAMESPACE: Final = re.compile(r"""\bxmlns(?::[\w.-]+)?\s*=\s*(["']).*?\1""", re.DOTALL)
ORIGIN: Final = re.compile(r"//")

# Everything a browser dereferences from inside an SVG. Only a same-document fragment
# is allowed; `sprite.svg#glyph` is a second file, and a second file is a second
# request that `collectstatic` hashing and the CSP would each have to be told about.
REFERENCE_ATTRIBUTES: Final = frozenset({"href", "src", "xlink:href"})


def _icon_partials() -> list[Path]:
    """Return every icon partial, refusing to return a set that proves nothing.

    The non-vacuity assertions belong here, not in a sentinel test: a sentinel is not
    selected by `pytest -k no_icon_partial_carries_a_script`, which is precisely when
    a rule reporting success over zero files gets believed. Gating the only entry
    point is what makes that impossible.

    `is_dir` keeps a missing directory a *finding* rather than an `OSError`; an
    erroring test is broken, not red, and in CI output the two look the same.
    """
    found = sorted(ICONS.glob("*.html")) if ICONS.is_dir() else []
    assert found, (
        f"no icon partials under {ICONS}: a glob that matches nothing satisfies every "
        "structural assertion below it, which is the quietest way for this guard to "
        "stop working"
    )

    # Non-empty alone is weak: one surviving file would keep all seven rules green
    # while fifteen call sites rendered nothing at all.
    missing = sorted(EXPECTED_ICONS - {path.stem for path in found})
    assert missing == [], (
        f"missing from {ICONS}: {missing}; found {sorted(p.stem for p in found)}"
    )
    return found


def _sources() -> list[tuple[Path, str]]:
    """Yield each icon partial paired with its source text."""
    return [(path, path.read_text(encoding="utf-8")) for path in _icon_partials()]


def _root_tag(text: str) -> str:
    """Return the opening `<svg …>` tag, so attributes are read off the root only.

    Scoping matters: `aria-hidden` on a child `<path>` hides nothing, and would
    otherwise satisfy a whole-file substring search.
    """
    match = ROOT_TAG.search(text)
    return match.group(0) if match else ""


def test_the_scan_reaches_every_named_icon_partial() -> None:
    """The contract, said out loud. `_icon_partials` is what enforces it."""
    # Given the icon directory
    found = _icon_partials()

    # Then it holds every glyph the interface names, by name. Asserted again here
    # rather than only in the scan, because this is the one place the rule reads as a
    # sentence instead of as somebody else's precondition.
    assert {path.stem for path in found} >= EXPECTED_ICONS


def test_every_icon_partial_is_one_inline_svg_and_nothing_else() -> None:
    """A partial is the glyph, not a wrapper around it.

    A stray `<span>` or a second `<svg>` would inherit none of `.icon`'s sizing and
    would break the moment the partial is included somewhere its parent differs.
    """
    offenders: list[str] = []
    for path, text in _sources():
        body = text.strip()
        count = len(SVG_OPEN.findall(body))
        if count != 1:
            offenders.append(f"{path.name}: {count} <svg> elements, expected exactly 1")
        elif not (SVG_OPEN.match(body) and body.lower().endswith("</svg>")):
            offenders.append(f"{path.name}: the <svg> is not the whole file")
    assert offenders == [], offenders


def test_every_icon_is_decorative_to_assistive_technology() -> None:
    """Both attributes, on the root, or the glyph is announced and focusable."""
    offenders: list[str] = []
    for path, text in _sources():
        root = dict(attributes_of(_root_tag(text)))
        if root.get("aria-hidden") != "true":
            offenders.append(f'{path.name}: root <svg> lacks aria-hidden="true"')
        # IE-era SVGs join the tab order without this, and a tab stop on a decoration
        # is a defect a keyboard user hits long before anyone else notices it.
        if root.get("focusable") != "false":
            offenders.append(f'{path.name}: root <svg> lacks focusable="false"')
    assert offenders == [], offenders


def test_every_icon_root_carries_the_design_system_class() -> None:
    """`.icon` is where the sizing lives; without it a glyph renders at 24px.

    `assets/css/app.css` gives `.icon` a `1em` box and `currentColor` stroke, so the
    class is what makes a pasted Lucide glyph take the size and colour of the text
    beside it instead of its own hard-coded `width="24"`.
    """
    offenders: list[str] = []
    for path, text in _sources():
        value = dict(attributes_of(_root_tag(text))).get("class", "")
        if "icon" not in value.split():
            offenders.append(f"{path.name}: root <svg> class={value!r}, wanted 'icon'")
    assert offenders == [], offenders


def test_no_icon_partial_carries_a_script() -> None:
    """SVG is a scripting host. `script-src 'self'` would allow this one inline."""
    offenders = [path.name for path, text in _sources() if SCRIPT.search(text)]
    assert offenders == [], offenders


def test_no_icon_partial_names_an_origin() -> None:
    """The whole reason icons are vendored as markup rather than fetched."""
    offenders = [
        f"{path.name}:{number}: {line.strip()}"
        for path, text in _sources()
        for number, line in enumerate(NAMESPACE.sub(" ", text).splitlines(), start=1)
        if ORIGIN.search(line)
    ]
    assert offenders == [], (
        "an icon names an external origin; paste the glyph's path data in instead"
    )


def test_no_icon_partial_dereferences_a_second_document() -> None:
    """A sprite reference is a request, and a request is an asset-pipeline problem."""
    offenders = [
        f"{path.name}: {name}={value!r}"
        for path, text in _sources()
        for name, value in attributes_of(text)
        if name in REFERENCE_ATTRIBUTES and not value.startswith("#")
    ]
    assert offenders == [], offenders


def test_no_icon_partial_embeds_a_data_uri() -> None:
    """A `data:` URI is a second asset smuggled past every check that reads paths."""
    offenders = [path.name for path, text in _sources() if DATA_URI.search(text)]
    assert offenders == [], offenders
