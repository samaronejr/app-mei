"""Structural primitives for the reflow contract both surfaces hold.

`templates_scan.py` next door reads every template and tokenises its attributes. This
module adds the one thing a reflow claim needs and that one cannot give: ENCLOSURE.
"Is this table inside a scroll container" is a question about ancestry, not about
which class names happen to appear somewhere in the same file, so the walk below
keeps a stack of open elements and hands every tag the ones containing it.

WHAT THIS IS NOT. It is not a snapshot of utility classes. Pinning `sm:grid-cols-2`
by its exact spelling turns a legitimate restyle red for no defect, and a suite that
cries wolf on a rename stops being read — which costs more than the drift it was
meant to catch. Every check here asks after a PROPERTY: is the table wrapped, is the
box still allowed to shrink, does the column count move at a breakpoint. The four
class names spelled literally below are the four the contracts are written in.

THE REVIEW WIDTHS ARE 360 / 768 / 1024 / 1440 (`REVIEW_WIDTHS`). Nothing in this file
measures a rendered pixel: a Django test client has no viewport and never will. Those
four numbers are the widths the STRUCTURE scanned here has to survive, and they are
the same four the optional screenshot pass reviews at — named once, in one place, so
that a scan and a screenshot cannot drift apart without somebody editing this tuple.

COMMENTS ARE BLANKED BEFORE ANY SCAN, and that is load-bearing rather than tidy. This
project explains itself in prose: `templates/base.html` discusses its own h1 rule and
`templates/core/dashboard.html` recounts the overflow bug it already fixed. A tag
walker reading those sentences pushes an element nothing ever closes, and every
enclosure decision after it in the file is then wrong. `class_tokens()` next door
deliberately does NOT blank them — a class name written into a note still has to
compile — which is exactly why this project's convention forbids spelling an
attribute inside one. The blanking here preserves newlines, so a reported line number
is the line an editor opens.
"""

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from django.apps import apps as django_apps

from tests.ui.templates_scan import TEMPLATE_SYNTAX, attributes_of, loaded_templates

# The widths this work is reviewed at, narrowest first. 360 is the phone this product
# is designed down to; 768 and 1024 straddle Tailwind's `md` and sit on `lg`; 1440 is
# the desk. Referenced by the cases below in prose and by the screenshot pass in fact.
REVIEW_WIDTHS: Final = (360, 768, 1024, 1440)

# The four class names the contracts are written in, and the only literals this module
# spells. Everything else is asked after as a property.
DATA_TABLE: Final = "data-table"
TABLE_WRAP: Final = "table-wrap"
ROW_STACK: Final = "row-stack"
CARD: Final = "card"

Template = tuple[Path, str]

# A `{% comment %}` block with its contents. See the module note for why every scan
# here blanks these and `class_tokens()` deliberately does not.
COMMENT_BLOCK: Final = re.compile(
    r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}",
    re.DOTALL,
)

# One HTML tag, open or closing. `[^>]*` for the attribute run is safe here because no
# attribute value in this project contains a `>`; the template conditionals that do
# (`{% if page.paginator.num_pages > 1 %}`) all sit between tags rather than inside one.
HTML_TAG: Final = re.compile(
    r"<(?P<closing>/?)(?P<name>[a-zA-Z][\w:-]*)(?P<attrs>[^>]*)>",
)

# Elements that never take a closing tag, so they must not be pushed onto the stack.
VOID_ELEMENTS: Final = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    },
)

# One table data cell. Read by regex rather than off the element walk because what
# matters here is an attribute the walk does not keep — `class` is all an Element
# carries, deliberately, so that the stack stays cheap on every template in the tree.
DATA_CELL: Final = re.compile(r"<td\b(?P<attrs>[^>]*)>", re.IGNORECASE)
COLUMN_LABEL: Final = "data-label"
ROW_SPAN: Final = "colspan"

# A width utility pinned to a literal pixel count. `max-w-*` is deliberately absent
# from the alternation: a MAXIMUM still lets the box shrink, which is the whole
# property this contract protects, and the four `max-w-*` values this project ships
# are the page measure itself.
FIXED_PIXEL_UTILITY: Final = re.compile(
    r"^(?:(?:sm|md|lg|xl|2xl):)*(?:w|min-w|basis|size)-\[[^\]]*px[^\]]*\]$",
)

# A CSS length written in pixels, for the inline-style and presentational-attribute
# halves of the same contract.
PIXEL_LENGTH: Final = re.compile(r"\b\d+(?:\.\d+)?px\b")

# Presentational width attributes. Spelled exactly, so `stroke-width="2"` on every
# inline icon is not mistaken for one: an SVG stroke is a line weight, not a box.
PRESENTATIONAL_WIDTH: Final = frozenset({"width", "min-width"})

# A `width:` or `min-width:` declaration inside a `style` attribute. Inline style is
# already refused project-wide by the policy guard; this is the second lock.
STYLE_WIDTH: Final = re.compile(r"(?:^|;)\s*(?:min-)?width\s*:", re.IGNORECASE)

# A grid column count, with the breakpoint it is declared behind if there is one. The
# breakpoint is the property every case here is actually reading.
GRID_COLUMNS: Final = re.compile(
    r"^(?:(?P<breakpoint>sm|md|lg|xl|2xl):)?grid-cols-(?P<count>\d+)$",
)

# The most columns a layout may declare unconditionally. Two is a label beside its
# value, which reflows down to 360 as two halves of a narrow line; three or more at
# the base breakpoint divides 360 minus the page gutters into columns too narrow to
# hold a CNPJ, a date or a currency figure without truncating it.
NARROW_COLUMN_CEILING: Final = 2

# The viewport declaration, and the half of it that actually enables reflow.
VIEWPORT_META: Final = re.compile(
    r"""<meta\b[^>]*\bname\s*=\s*(["'])viewport\1[^>]*>""",
    re.IGNORECASE,
)
DEVICE_WIDTH: Final = "width=device-width"

# Declarations that take pinch-zoom away from a reader who needs it (WCAG 1.4.4). A
# viewport tag carrying one of these is present and still wrong, so presence alone is
# never what the shell cases assert.
ZOOM_LOCKS: Final = ("user-scalable=no", "user-scalable=0", "maximum-scale=1")


@dataclass(frozen=True)
class Element:
    """One open tag: its name, the literal classes on it, and where it was written."""

    name: str
    classes: frozenset[str]
    path: Path
    line: int

    def __str__(self) -> str:
        """Render as `path:line <tag>`, which an editor can jump to."""
        return f"{self.path.as_posix()}:{self.line} <{self.name}>"


@dataclass(frozen=True)
class ColumnDeclaration:
    """A `grid-cols-N` utility, and the breakpoint it is declared behind if any."""

    element: Element
    token: str
    breakpoint: str
    count: int

    def __str__(self) -> str:
        """Render as `path:line <tag> token`."""
        return f"{self.element} {self.token}"


def blank_comments(text: str) -> str:
    """Replace each `{% comment %}` block with its own newlines and nothing else.

    Newline-preserving on purpose: every position reported by this module is turned
    into a line number, and a scan that reported the wrong line would send a reader
    to innocent markup and teach them to distrust the whole file.
    """
    return COMMENT_BLOCK.sub(lambda block: "\n" * block.group(0).count("\n"), text)


def classes_on(attrs: str) -> frozenset[str]:
    """Return the literal class names written on one tag's attribute run.

    Template syntax is stripped before tokenising rather than filtered afterwards, for
    the reason `class_tokens()` gives: filtering afterwards mistakes a tag's own words
    for class names, while stripping first keeps the literals on both sides of a
    conditional — and both sides are laid out at every width.
    """
    for name, value in attributes_of(attrs):
        if name == "class":
            return frozenset(TEMPLATE_SYNTAX.sub(" ", value).split())
    return frozenset()


def elements_of(path: Path, text: str) -> Iterator[tuple[Element, tuple[Element, ...]]]:
    """Yield every open tag in one template, paired with the elements enclosing it.

    A closing tag unwinds to the nearest matching open element rather than popping
    blindly, so a stray `</div>` costs one frame instead of derailing the rest of the
    file. Self-closing and void tags are never pushed.
    """
    body = blank_comments(text)
    stack: list[Element] = []
    for match in HTML_TAG.finditer(body):
        name = match.group("name").lower()
        attrs = match.group("attrs")
        if match.group("closing"):
            for depth in range(len(stack) - 1, -1, -1):
                if stack[depth].name == name:
                    del stack[depth:]
                    break
            continue
        element = Element(
            name=name,
            classes=classes_on(attrs),
            path=path,
            line=body.count("\n", 0, match.start()) + 1,
        )
        yield element, tuple(stack)
        if name not in VOID_ELEMENTS and not attrs.rstrip().endswith("/"):
            stack.append(element)


def _portal_root() -> Path:
    """Return the directory the portal's own templates live in."""
    return Path(django_apps.get_app_config("portal").path) / "templates"


def portal_templates() -> list[Template]:
    """Return every template rendered under the portal's standalone shell."""
    root = _portal_root()
    return [pair for pair in loaded_templates() if pair[0].is_relative_to(root)]


def firm_templates() -> list[Template]:
    """Return every template rendered under the firm's shell."""
    root = _portal_root()
    return [pair for pair in loaded_templates() if not pair[0].is_relative_to(root)]


def _gate_walk(templates: Sequence[Template], *, surface: str) -> None:
    """Refuse a scan that reached no files.

    Shared by every helper below rather than written as a case of its own, because a
    standalone sentinel is one `pytest -k` away from being deselected while the cases
    that depend on it keep running green over nothing.
    """
    assert templates, (
        f"the {surface} template scan reached no files at all, so every claim it "
        f"supports below is a statement about the empty set rather than about markup"
    )


def data_tables(
    templates: Sequence[Template],
    *,
    surface: str,
    minimum: int,
) -> list[tuple[Element, tuple[Element, ...]]]:
    """Return every `.data-table` on a surface, with the elements enclosing it.

    TWO NON-VACUITY GATES, both here rather than in cases of their own. They exist
    because the empty list reads exactly like "every table on this surface reflows",
    whichever of three quite different things produced it: a scan that reached no
    files, a surface that lost its tables, or a class rename that made the scan blind
    to the ones still there. Only the first is obviously a bug from outside, and the
    third is the one this whole file is most likely to suffer.
    """
    _gate_walk(templates, surface=surface)
    found = [
        (element, ancestors)
        for path, text in templates
        for element, ancestors in elements_of(path, text)
        if element.name == "table" and DATA_TABLE in element.classes
    ]
    assert len(found) >= minimum, (
        f"the {surface} scan found {len(found)} elements carrying .{DATA_TABLE} and "
        f"expected at least {minimum}; a wrapping rule quantified over no tables "
        f"passes for a surface that renders every column off the side of a phone"
    )
    return found


def unreflowed(
    tables: Sequence[tuple[Element, tuple[Element, ...]]],
) -> list[str]:
    """Return every table that neither scrolls inside a wrapper nor stacks.

    The two treatments are alternatives rather than a pair, because they answer the
    same question differently: `.table-wrap` keeps a wide table reachable by giving
    it its own scroll container, and `.row-stack` dissolves the columns into
    labelled blocks below `sm`. Either one keeps the page itself from being pushed
    sideways at 360, which is the property under test.
    """
    return [
        str(element)
        for element, ancestors in tables
        if ROW_STACK not in element.classes
        and not any(TABLE_WRAP in ancestor.classes for ancestor in ancestors)
    ]


def cards_in_lists(
    templates: Sequence[Template],
    *,
    surface: str,
    minimum: int,
) -> list[Element]:
    """Return every `<li>` drawn as a card — a record that stacks instead of tabulating.

    NON-VACUITY GATE. This is the counterweight to asserting that a surface ships no
    table at all: "no tables here" is trivially true of a surface that ships no
    records either, and would stay green through a page that had quietly stopped
    rendering its list. The floor makes the absence mean what it is meant to mean.
    """
    _gate_walk(templates, surface=surface)
    found = [
        element
        for path, text in templates
        for element, _ in elements_of(path, text)
        if element.name == "li" and CARD in element.classes
    ]
    assert len(found) >= minimum, (
        f"the {surface} scan found {len(found)} record cards and expected at least "
        f"{minimum}; an assertion that this surface tabulates nothing says nothing "
        f"at all while it also lists nothing"
    )
    return found


def unlabelled_cells(
    templates: Sequence[Template],
    *,
    surface: str,
    minimum: int,
) -> list[str]:
    """Return every data cell that would stack into a bare, unnamed value.

    The other half of the `.row-stack` treatment, and the half that fails quietly.
    Below `sm` the header row is hidden from sight and each cell regrows its column
    name out of `data-label`, so a cell without one becomes a figure on a phone with
    nothing saying whether it is a ceiling, a total or a percentage. A cell spanning
    the whole row is exempt: it has no single column to name.

    Asked of EVERY data cell rather than only of those written inside a stacking
    table, because the row markup and the table markup are different files bound by a
    name held on a queue spec — the cell cannot see which table will draw it, so it
    has to be legible in either.

    NON-VACUITY GATE: the walk must have found at least `minimum` cells. An empty
    result means "no cell stacks into a bare value" and is equally true of a surface
    with no cells at all, which is what a broken scan looks like from outside.
    """
    _gate_walk(templates, surface=surface)
    seen = 0
    offenders: list[str] = []
    for path, text in templates:
        body = blank_comments(text)
        for cell in DATA_CELL.finditer(body):
            seen += 1
            named = {name for name, _ in attributes_of(cell.group("attrs"))}
            if COLUMN_LABEL in named or ROW_SPAN in named:
                continue
            line = body.count("\n", 0, cell.start()) + 1
            offenders.append(f"{path.as_posix()}:{line} <td>")
    assert seen >= minimum, (
        f"the {surface} scan found {seen} data cells and expected at least {minimum}; "
        f"a labelling rule that reached no cells is green whatever the markup says"
    )
    return offenders


def fixed_pixel_widths(
    templates: Sequence[Template],
    *,
    surface: str,
    minimum: int,
) -> list[str]:
    """Return every declaration pinning a box to a literal pixel count.

    Three shapes are read, because a pixel width can be written three ways: a Tailwind
    arbitrary value on a width utility, a `width` or `min-width` presentational
    attribute, and a `width:` inside an inline style. `max-w-*` is excluded by
    construction — a maximum still lets the box shrink, and shrinking is the property.
    A rem-scale `w-*` or `min-w-*` is allowed for the mirror-image reason: it tracks
    the root font size, so it grows with a reader's text-size setting instead of
    clipping underneath it. What is refused is the literal pixel, which honours
    neither the viewport at 360 nor the reader who enlarged their type.

    NON-VACUITY GATE: the scan must have read at least `minimum` class attributes.
    Without it, a walk that parsed nothing — a regex edited into uselessness, a
    surface whose files stopped being discovered — reports "no fixed widths" in the
    same words as a surface that genuinely has none.
    """
    _gate_walk(templates, surface=surface)
    offenders: list[str] = []
    seen_class_attributes = 0
    for path, text in templates:
        body = blank_comments(text)
        for name, value in attributes_of(body):
            where = f"{path.as_posix()}: {name}={value.strip()[:60]!r}"
            if name == "class":
                seen_class_attributes += 1
                literal = TEMPLATE_SYNTAX.sub(" ", value)
                offenders.extend(
                    f"{path.as_posix()}: {token}"
                    for token in literal.split()
                    if FIXED_PIXEL_UTILITY.match(token)
                )
            elif name in PRESENTATIONAL_WIDTH:
                # A presentational width attribute is unitless CSS pixels by
                # definition, so any value but a percentage is a fixed one.
                if value.strip() and not value.strip().endswith("%"):
                    offenders.append(where)
            elif name == "style" and STYLE_WIDTH.search(value):
                if PIXEL_LENGTH.search(value):
                    offenders.append(where)
    assert seen_class_attributes >= minimum, (
        f"the {surface} scan read {seen_class_attributes} class attributes and "
        f"expected at least {minimum}; a width rule applied to a parse that found no "
        f"classes is green by construction"
    )
    return offenders


def column_declarations(
    templates: Sequence[Template],
    *,
    surface: str,
    minimum: int,
) -> list[ColumnDeclaration]:
    """Return every `grid-cols-N` written on a surface, breakpoint included.

    NON-VACUITY GATE. Every case reading this list argues about WHERE a column count
    is declared, and an empty list satisfies all of them at once — "nothing lays out
    three columns before a breakpoint" is perfectly true of a surface that lays out
    nothing. The floor is what stops a renamed utility, a dropped grid or a broken
    scan from being reported as compliance.
    """
    _gate_walk(templates, surface=surface)
    found: list[ColumnDeclaration] = []
    for path, text in templates:
        for element, _ in elements_of(path, text):
            for token in sorted(element.classes):
                match = GRID_COLUMNS.match(token)
                if match is None:
                    continue
                found.append(
                    ColumnDeclaration(
                        element=element,
                        token=token,
                        breakpoint=match.group("breakpoint") or "",
                        count=int(match.group("count")),
                    ),
                )
    assert len(found) >= minimum, (
        f"the {surface} scan found {len(found)} grid column declarations and expected "
        f"at least {minimum}; a rule about where a column count may be declared is "
        f"vacuous on a surface where none is declared anywhere"
    )
    return found


def viewport_of(shell: Path, *, surface: str) -> str:
    """Return the viewport meta tag one shell declares.

    GATED THREE WAYS, because this contract's failure mode is silence: a shell whose
    path drifted, a shell emptied by a bad edit and a shell that simply lost the tag
    are indistinguishable from outside, and all three end with a document that lays
    itself out at 980 CSS pixels and is then scaled down — every figure on it too
    small to read and every control too small to hit, at exactly the width
    (360) this product is designed for.
    """
    assert shell.is_file(), (
        f"the {surface} shell is not at {shell.as_posix()}; the path this case reads "
        f"has drifted, so it can no longer tell a compliant shell from a missing one"
    )
    text = blank_comments(shell.read_text(encoding="utf-8"))
    assert text.strip(), (
        f"the {surface} shell at {shell.as_posix()} is empty once its notes are "
        f"blanked, so there is no markup here to hold any contract"
    )
    found = VIEWPORT_META.search(text)
    assert found is not None, (
        f"the {surface} shell at {shell.as_posix()} declares no viewport meta tag, so "
        f"every page it wraps is laid out at a desktop width and scaled down on a "
        f"phone — the layout below is then reflowing against a viewport that is not "
        f"the device's"
    )
    return found.group(0)


__all__ = [
    "CARD",
    "COLUMN_LABEL",
    "DATA_TABLE",
    "DEVICE_WIDTH",
    "HTML_TAG",
    "NARROW_COLUMN_CEILING",
    "REVIEW_WIDTHS",
    "ROW_STACK",
    "TABLE_WRAP",
    "VOID_ELEMENTS",
    "ZOOM_LOCKS",
    "ColumnDeclaration",
    "Element",
    "Template",
    "blank_comments",
    "cards_in_lists",
    "classes_on",
    "column_declarations",
    "data_tables",
    "elements_of",
    "firm_templates",
    "fixed_pixel_widths",
    "portal_templates",
    "unlabelled_cells",
    "unreflowed",
    "viewport_of",
]
