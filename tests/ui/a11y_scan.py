"""Structural primitives for the one accessibility claim a rendered page cannot make.

`templates_scan.py` reads every template and tokenises its attributes.
`responsive_scan.py` adds ENCLOSURE — which elements contain which. This module adds
the third thing an accessibility contract needs and neither of those gives: CONTENT.
"Does this control say anything at all" is a question about what sits BETWEEN a tag
and its close, not about which classes appear on it or which elements wrap it.

WHAT THIS PROTECTS. A `<button>` or `<a>` whose only child is an icon include is
silent. Every icon partial this project ships is `aria-hidden="true"` by construction
and `tests/ui/test_icons.py` pins that — which is correct, an icon beside a word is
decoration — but it means a control with nothing but an icon inside it has NO
accessible name whatsoever. A screen reader announces "button", and nothing else. The
person hearing it is told a control exists and is told nothing about what it does.

The remedy this project already ships is `.visually-hidden`: reachable by a screen
reader, invisible to everyone else, defined in `assets/css/app.css` with a comment
naming this exact use. So the rule is: a control carrying an icon must carry a name
too — visible text, hidden text, or an `aria-label` — and the scan below refuses the
control that carries none.

THE SURFACE SHIPS NO OFFENDER TODAY, and that is precisely why the detector is
gated by a PLANTED POSITIVE rather than by a floor alone. An empty offender list is
what a compliant surface produces, and it is equally what a regex edited into
uselessness produces, and the two are indistinguishable from outside. So
`unnamed_icon_controls()` first runs a synthetic pair through its own machinery — one
fragment that must be reported and one that must not — before it reports anything
about the real templates. Both gates live inside the helper, where `pytest -k` cannot
deselect them.

COMMENTS ARE BLANKED FIRST, through `responsive_scan.blank_comments`, reused rather
than reimplemented. This project explains itself in prose and several of those notes
quote markup: `apps/portal/templates/portal/partials/nav.html` discusses the very rule
this module enforces, in a paragraph that names anchors and icons. A walker reading
those sentences pushes elements nothing closes and every span after it is wrong.
"""

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from tests.ui.responsive_scan import (
    HTML_TAG,
    VOID_ELEMENTS,
    Template,
    blank_comments,
)

# The elements a keyboard lands on and a screen reader announces as a control. `<a>`
# and `<button>` are the two the contract is written about; `<summary>` is included
# because it is a real button to the browser before any script has run — the firm
# shell's navigation disclosure is one — and it fails in exactly the same way.
CONTROL_ELEMENTS: Final = frozenset({"a", "button", "summary"})

# `{% include "partials/pure/icons/<name>.html" %}`, either quote style. This is the
# whole icon mechanism: there is no `{% icon %}` tag, every glyph is a partial under
# that one directory, and each of them is a single inline `<svg aria-hidden="true">`.
ICON_INCLUDE: Final = re.compile(
    r"""\{%\s*include\s+(["'])partials/pure/icons/[\w.-]+\.html\1[^%]*%\}""",
)

# An inline `<svg>` written straight into a control rather than included. Caught for
# the same reason and by the same rule: the partials are all marked decorative, and a
# hand-written one that is not is still not a name.
SVG_ELEMENT: Final = re.compile(r"<svg\b.*?</svg\s*>", re.DOTALL | re.IGNORECASE)
SVG_OPEN: Final = re.compile(r"<svg\b", re.IGNORECASE)

# Template constructs that put READABLE TEXT into the document. Replaced with a
# sentinel rather than stripped, because `{% translate "Baixar" %}` is the label — the
# byte string is decided at render time, but that a string arrives is decided here.
TEXT_PRODUCING: Final = re.compile(
    r"\{%\s*(?:translate|trans|blocktranslate)\b.*?%\}|\{\{.*?\}\}",
    re.DOTALL,
)

# Every other template tag: `{% if %}`, `{% for %}`, `{% endblocktranslate %}`. These
# render nothing themselves, so they are removed rather than counted as text.
CONTROL_FLOW_TAG: Final = re.compile(r"\{%.*?%\}", re.DOTALL)

# Any HTML tag, once the icons and the template syntax are gone. Removing these is
# what leaves the text nodes behind, `.visually-hidden` spans included — the hidden
# label IS the accessible name, so its content must survive this step.
ANY_TAG: Final = re.compile(r"<[^>]*>", re.DOTALL)

# The stand-in for "a translated string lands here". A private-use character, so it
# can never collide with something literally written in a template.
TEXT_SENTINEL: Final = "\ue000"

# The attributes that name a control without giving it any content at all.
NAMING_ATTRIBUTES: Final = ("aria-label", "title")

ATTRIBUTE_VALUE: Final = r"""\s*=\s*["']([^"']*)["']"""

# The class this project labels a silent control with. Spelled once, here.
HIDDEN_LABEL: Final = "visually-hidden"

# The planted pair. Run through this module's own machinery by `unnamed_icon_controls`
# before it reports anything real, so that "no offenders" is a statement the detector
# has just demonstrated it is capable of contradicting.
PLANTED_UNNAMED: Final = (
    '<button type="submit" class="btn">'
    '{% include "partials/pure/icons/x-circle.html" %}'
    "</button>"
)
PLANTED_NAMED: Final = (
    '<button type="submit" class="btn">'
    '{% include "partials/pure/icons/x-circle.html" %}'
    f'<span class="{HIDDEN_LABEL}">{{% translate "Fechar" %}}</span>'
    "</button>"
)
PLANTED_PATH: Final = Path("<planted>")


@dataclass(frozen=True)
class Control:
    """One control, its opening tag, and everything written between it and its close."""

    name: str
    path: Path
    line: int
    opening: str
    inner: str

    def __str__(self) -> str:
        """Render as `path:line <tag>`, which an editor can jump to."""
        return f"{self.path.as_posix()}:{self.line} <{self.name}>"

    @property
    def carries_an_icon(self) -> bool:
        """Whether a glyph is drawn inside this control, included or inline."""
        return bool(
            ICON_INCLUDE.search(self.inner) or SVG_OPEN.search(self.inner),
        )

    @property
    def readable_text(self) -> str:
        """Return whatever text this control puts in front of a reader.

        Icons come out first, then the svg elements they expand into, so a glyph is
        never mistaken for a word. What remains of the template syntax is split in
        two: the constructs that emit a string become a sentinel, and the ones that
        only branch are dropped. Removing the HTML tags last is what leaves the text
        nodes — and a `.visually-hidden` span's content is a text node like any other,
        which is the point: it is invisible to a reader and is still a name.
        """
        text = ICON_INCLUDE.sub(" ", self.inner)
        text = SVG_ELEMENT.sub(" ", text)
        text = TEXT_PRODUCING.sub(TEXT_SENTINEL, text)
        text = CONTROL_FLOW_TAG.sub(" ", text)
        return ANY_TAG.sub(" ", text).strip()

    @property
    def named_by_attribute(self) -> str:
        """Return the first non-empty `aria-label` or `title` on the opening tag."""
        for attribute in NAMING_ATTRIBUTES:
            found = re.search(
                rf"\b{re.escape(attribute)}{ATTRIBUTE_VALUE}",
                self.opening,
                re.IGNORECASE,
            )
            if found is not None and found.group(1).strip():
                return found.group(1)
        return ""

    @property
    def has_an_accessible_name(self) -> bool:
        """Whether anything at all would be announced when this control is reached."""
        return bool(self.readable_text or self.named_by_attribute)


def controls_of(path: Path, text: str) -> Iterator[Control]:
    """Yield every control in one template, paired with the content it encloses.

    A closing tag unwinds to the nearest matching open element rather than popping
    blindly, exactly as `responsive_scan.elements_of` does, so a stray `</div>` costs
    one frame instead of derailing the rest of the file. Every frame discarded by an
    unwind is yielded if it is a control, with this close as its end: widening a span
    can only hide an offender inside a broken document, never invent one, while
    dropping the frame would take a real control out of the scan entirely.

    A control left unclosed at the end of the file is not yielded. That is deliberate
    — there is no honest end position for it — and it is why the floor below counts
    the controls the walk DID close.
    """
    body = blank_comments(text)
    stack: list[tuple[str, int, int, str]] = []
    for match in HTML_TAG.finditer(body):
        name = match.group("name").lower()
        attrs = match.group("attrs")
        if match.group("closing"):
            for depth in range(len(stack) - 1, -1, -1):
                if stack[depth][0] != name:
                    continue
                for element, start, opened, opening in stack[depth:]:
                    if element in CONTROL_ELEMENTS:
                        yield Control(
                            name=element,
                            path=path,
                            line=body.count("\n", 0, start) + 1,
                            opening=opening,
                            inner=body[opened : match.start()],
                        )
                del stack[depth:]
                break
            continue
        if name in VOID_ELEMENTS or attrs.rstrip().endswith("/"):
            continue
        stack.append((name, match.start(), match.end(), match.group(0)))


def _silent(controls: Sequence[Control]) -> list[str]:
    """Return every control that draws a glyph and announces nothing."""
    return [
        str(control)
        for control in controls
        if control.carries_an_icon and not control.has_an_accessible_name
    ]


def _gate_detector() -> None:
    """Prove the scan can still tell a named control from a silent one.

    THE GATE THIS MODULE EXISTS FOR. Every case reading `unnamed_icon_controls` asserts
    that a list is empty, and this surface produces the empty list honestly — there is
    no icon-only control in the tree today. That makes compliance and total blindness
    the same observation. So a synthetic pair is pushed through the identical code path
    first: one fragment that must be reported, one that differs only by carrying a
    `.visually-hidden` label and must not be. A regex broken in either direction fails
    here, by name, before a single real template is read.
    """
    reported = _silent(list(controls_of(PLANTED_PATH, PLANTED_UNNAMED)))
    assert len(reported) == 1, (
        f"the planted icon-only control — {PLANTED_UNNAMED} — was reported "
        f"{len(reported)} times rather than once, so this scan can no longer detect "
        f"the defect it exists to detect and every empty result below means nothing"
    )
    missed = _silent(list(controls_of(PLANTED_PATH, PLANTED_NAMED)))
    assert missed == [], (
        f"the planted control carrying a .{HIDDEN_LABEL} label — {PLANTED_NAMED} — was "
        f"reported as silent, so this scan refuses the very remedy it asks for and "
        f"would redden a compliant template: {missed}"
    )


def controls_carrying_icons(
    templates: Sequence[Template],
    *,
    surface: str,
    minimum: int,
) -> list[Control]:
    """Return every control on a surface that draws a glyph inside itself.

    TWO NON-VACUITY GATES, both here rather than in cases of their own. The walk must
    have reached files, and it must have found at least `minimum` controls with icons
    in them. Without the second, a renamed icon directory or an include spelled a new
    way would make the detector blind to every control at once and report that as
    compliance — which is the single most likely way this file stops working.
    """
    assert templates, (
        f"the {surface} template scan reached no files at all, so every claim it "
        f"supports is a statement about the empty set rather than about markup"
    )
    found = [
        control
        for path, text in templates
        for control in controls_of(path, text)
        if control.carries_an_icon
    ]
    assert len(found) >= minimum, (
        f"the {surface} scan found {len(found)} controls drawing an icon and expected "
        f"at least {minimum}; a naming rule quantified over no controls is green "
        f"whatever the markup says"
    )
    return found


def unnamed_icon_controls(
    templates: Sequence[Template],
    *,
    surface: str,
    minimum: int,
) -> list[str]:
    """Return every control that draws a glyph and announces nothing at all.

    The detector proves itself against the planted pair before the surface is read,
    and `controls_carrying_icons` refuses a walk that found nothing. Both gates run
    inside this call, so a case that reaches the assertion has already established
    that an empty answer is a finding rather than a silence.
    """
    _gate_detector()
    return _silent(controls_carrying_icons(templates, surface=surface, minimum=minimum))


__all__ = [
    "CONTROL_ELEMENTS",
    "HIDDEN_LABEL",
    "ICON_INCLUDE",
    "PLANTED_NAMED",
    "PLANTED_UNNAMED",
    "Control",
    "controls_carrying_icons",
    "controls_of",
    "unnamed_icon_controls",
]
