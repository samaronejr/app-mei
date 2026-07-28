"""Read every template this project ships, for the guard tests that scan them.

Discovery walks `TEMPLATES[0]["DIRS"]` plus each installed app's `templates/`
directory, rather than a hard-coded list, so a template added in a later wave is
covered the moment it lands instead of when someone remembers to extend a tuple.
"""

import re
from collections.abc import Iterator
from pathlib import Path

from django.apps import apps as django_apps
from django.conf import settings

# Attribute values on a tag, e.g. src="..." or hx-get='...'. Both quote styles, and
# the value captured without them.
ATTRIBUTE = re.compile(r"""([a-zA-Z0-9:_.-]+)\s*=\s*(["'])(.*?)\2""", re.DOTALL)

# A Django variable interpolation. Distinguished from {% tag %} deliberately: a tag
# is server-authored, a variable can carry whatever a tenant typed into a name field.
VARIABLE = re.compile(r"\{\{.*?\}\}")

# Any Django tag or variable. Removed from a class attribute before tokenising it,
# so a tag's own words are never mistaken for class names.
TEMPLATE_SYNTAX = re.compile(r"\{%.*?%\}|\{\{.*?\}\}", re.DOTALL)

# A scheme-relative or absolute URL. `//evil.example` is the one people forget.
EXTERNAL_URL = re.compile(r"""^\s*(?:[a-zA-Z][a-zA-Z0-9+.-]*:)?//""")


def template_files() -> list[Path]:
    """Return every template file reachable by the configured loaders."""
    roots: list[Path] = [Path(directory) for directory in settings.TEMPLATES[0]["DIRS"]]
    roots.extend(
        Path(config.path) / "templates"
        for config in django_apps.get_app_configs()
        if config.name.startswith("apps.")
    )
    found: list[Path] = []
    for root in roots:
        if root.is_dir():
            found.extend(sorted(root.rglob("*.html")))
    return found


def attributes_of(text: str) -> Iterator[tuple[str, str]]:
    """Yield every `(name, value)` attribute pair in a template's source."""
    for match in ATTRIBUTE.finditer(text):
        yield match.group(1).lower(), match.group(3)


def loaded_templates() -> Iterator[tuple[Path, str]]:
    """Yield each template path paired with its source text."""
    for path in template_files():
        yield path, path.read_text(encoding="utf-8")


def class_tokens() -> set[str]:
    """Return every literal class name written in a template's `class` attribute.

    Template syntax is stripped before tokenising rather than filtered afterwards.
    Filtering afterwards mistakes a tag's own words — `if`, `==`, a quoted comparison
    value — for class names, while stripping first keeps the literals on *both* sides
    of a conditional, which are exactly the ones that need a compiled rule.
    """
    tokens: set[str] = set()
    for _, text in loaded_templates():
        for name, value in attributes_of(text):
            if name != "class":
                continue
            literal = TEMPLATE_SYNTAX.sub(" ", value)
            tokens.update(token for token in literal.split() if token)
    return tokens


# The characters CSS requires escaping in an identifier. Tailwind writes selectors
# this way, so a token must be transformed the same way before it can be looked up.
_NEEDS_ESCAPE = set("!\"#$%&'()*+,./:;<=>?@[\\]^`{|}~ ")


def css_selector_for(token: str) -> str:
    """Return the selector Tailwind would emit for a utility class name."""
    escaped = "".join(f"\\{ch}" if ch in _NEEDS_ESCAPE else ch for ch in token)
    return f".{escaped}"


__all__ = [
    "EXTERNAL_URL",
    "TEMPLATE_SYNTAX",
    "VARIABLE",
    "attributes_of",
    "class_tokens",
    "css_selector_for",
    "loaded_templates",
    "template_files",
]
