"""Frozen 329a98c redactor; unchanged discovery helpers remain shared.

Only the known-value scan and its interval collection are under comparison.
Keep this reference's function body identical to the approved revision.
"""

import re
from collections.abc import Callable

from apps.integrations.privacy import MARKER, _remember, _spans, _walk


def redact_structure(
    value: object,
    credentials: set[str],
    *,
    fields: frozenset[str] | None = frozenset(),
    urls: Callable[[str], tuple[str, set[str]]] | None = None,
) -> object:
    """Discover first, replace second, across the entire record and all its keys."""
    plans: dict[str, tuple[str, list[tuple[int, int]]]] = {}

    def discover(item: object, hidden: bool) -> object:
        if isinstance(item, (str, bytes)):
            text = (
                item.decode("utf-8", "surrogateescape")
                if isinstance(item, bytes)
                else item
            )
            if hidden:
                _remember(text, credentials)
            redacted, found = urls(text) if urls else (text, set())
            credentials.update(found)
            spans = _spans(redacted, fields, credentials)
            plans[text] = (redacted, spans)
        return item

    _walk(value, discover, fields)
    # A marker is an atomic alternative, never a prefix exemption. Longest values
    # win over overlapping short credentials, including marker-prefixed secrets.
    alternatives = sorted((credentials - {""}) | {MARKER}, key=len, reverse=True)
    # A lookahead consumes nothing, so occurrences that overlap an earlier match
    # are found too; _union() then removes every part of them.
    pattern = re.compile(
        "(?=(" + "|".join(re.escape(item) for item in alternatives) + "))"
    )

    def replace(item: object, hidden: bool) -> object:
        if hidden:
            return MARKER
        if not isinstance(item, (str, bytes)):
            return item
        text = (
            item.decode("utf-8", "surrogateescape") if isinstance(item, bytes) else item
        )
        redacted, spans = plans[text]
        # Merge known-value matches with structural spans in original coordinates.
        # Mutating a URL's signature first would otherwise hide its full known path.
        spans = [*spans, *(match.span(1) for match in pattern.finditer(redacted))]
        pieces: list[str] = []
        cursor = 0
        for start, end in _union(spans):
            pieces.extend((redacted[cursor:start], MARKER))
            cursor = end
        pieces.append(redacted[cursor:])
        redacted = "".join(pieces)
        return (
            redacted.encode("utf-8", "surrogateescape")
            if isinstance(item, bytes)
            else redacted
        )

    return _walk(value, replace, fields)


def _union(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Frozen 329a98c range union, independent of the current call site."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
