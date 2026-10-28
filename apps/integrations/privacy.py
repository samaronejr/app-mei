"""Structural, two-pass redaction shared by console and Sentry."""

import ast
import json
import re
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

MARKER: Final = "[Filtered]"
# Bound both discovery text and trie nodes, not a prefix of private input.
_MAX_SCAN_CHARACTERS: Final = 512 * 1024
_SCAN_BUDGET_ERROR: Final = "redaction diagnostic exceeds its character budget"


class RedactionBudgetError(ValueError):
    """The entire diagnostic must be withheld instead of partly scanned."""


PROVIDER_FIELDS: Final = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "jwt_token",
        "access_token",
        "autenticar_procurador_token",
        "token",
        "body",
        "data",
        "payload",
    }
)
_PUNCTUATION: Final = frozenset("{}[](),:=;?&#<>")
_CLOSING: Final = {"{": "}", "[": "]", "(": ")"}
_SIGNED_URL: Final = re.compile(
    r"""https?://[^\s"'<>]*[?&](?:X-Amz-[^=\s]+|Signature|AWSAccessKeyId)=[^\s"'<>]*""",
    re.IGNORECASE,
)
_SIGNED_QUERY: Final = re.compile(
    r"(?i)([?&](?:X-Amz-[A-Za-z-]+|Signature|AWSAccessKeyId)=)([^&#\s\"']+)"
)
_BASE64: Final = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{1025,}={0,2}")


@dataclass(frozen=True, slots=True)
class _Token:
    start: int
    end: int
    value: str
    content_start: int
    content_end: int


def _literal(value: str) -> str:
    """Decode only an individual str/bytes literal, never executable expressions."""
    if not value.startswith(("'", '"', "b'", 'b"', "B'", 'B"')):
        return value
    try:
        parsed = json.loads(value) if value.startswith('"') else ast.literal_eval(value)
    except (ValueError, SyntaxError, SyntaxWarning):
        return value
    match parsed:
        case bytes():
            return parsed.decode("utf-8", "surrogateescape")
        case str():
            return parsed
        case _:
            return value


def _tokens(text: str) -> list[_Token]:
    """Find actual closing quotes, skipping escaped characters atomically."""
    tokens: list[_Token] = []
    cursor = 0
    while cursor < len(text):
        if text[cursor].isspace():
            cursor += 1
            continue
        start = cursor
        quote_at = cursor + int(
            text[cursor] in "bB" and text[cursor + 1 : cursor + 2] in {"'", '"'}
        )
        if text[quote_at] in {"'", '"'}:
            quote = text[quote_at]
            cursor = quote_at + 1
            while cursor < len(text):
                if text[cursor] == "\\":
                    cursor += 2
                elif text[cursor] == quote:
                    break
                else:
                    cursor += 1
            content_end = min(cursor, len(text))
            cursor = min(cursor + 1, len(text))
            raw = text[start:cursor]
            tokens.append(
                _Token(start, cursor, _literal(raw), quote_at + 1, content_end)
            )
            continue
        if text.startswith(MARKER, cursor):
            cursor += len(MARKER)
        elif text[cursor] in _PUNCTUATION:
            cursor += 1
        else:
            while (
                cursor < len(text)
                and not text[cursor].isspace()
                and text[cursor] not in _PUNCTUATION
                and text[cursor] not in {"'", '"'}
            ):
                cursor += 1
        tokens.append(_Token(start, cursor, text[start:cursor], start, cursor))
    return tokens


def field_name(name: str | bytes) -> str:
    """Normalize actual bytes keys and the SDK's serialized bytes-key literals."""
    text = name.decode("latin-1") if isinstance(name, bytes) else name
    return _literal(text).lower()


def provider_field(name: str | bytes) -> bool:
    """Recognize provider credentials and every X-*Token* header."""
    lowered = field_name(name)
    return lowered in PROVIDER_FIELDS or (
        lowered.startswith("x-") and "token" in lowered
    )


def _sensitive(value: object, fields: frozenset[str] | None) -> bool:
    return (
        fields is not None
        and isinstance(value, (str, bytes))
        and (provider_field(value) or field_name(value) in fields)
    )


def _container_end(text: str, tokens: list[_Token]) -> int:
    closing: list[str] = []
    for item in tokens:
        if item.content_start != item.start:
            continue
        if item.value in _CLOSING:
            closing.append(_CLOSING[item.value])
        elif item.value in _CLOSING.values():
            if not closing or closing.pop() != item.value:
                return len(text)
            if not closing:
                return item.end
    return len(text)


def _value_span(text: str, tokens: list[_Token], index: int) -> tuple[int, int]:
    token = tokens[index]
    if token.content_start != token.start:
        return token.content_start, token.content_end
    if token.value in _CLOSING:
        return token.start, _container_end(text, tokens[index:])
    end = token.start
    while end < len(text):
        if text.startswith(MARKER, end):
            end += len(MARKER)
        elif text[end].isspace() or text[end] in "',;\"}])&#":
            break
        else:
            end += 1
    return token.start, end


def _spans(
    text: str, fields: frozenset[str] | None, credentials: set[str]
) -> list[tuple[int, int]]:
    tokens = _tokens(text)
    spans: list[tuple[int, int]] = []
    for index, token in enumerate(tokens):
        following = index + 1
        if _sensitive(token.value, fields):
            if following >= len(tokens) or tokens[following].value not in {
                ":",
                "=",
                ",",
            }:
                continue
            following += 1
        elif token.value.lower() not in {"bearer", "basic"}:
            continue
        if following < len(tokens):
            if tokens[following].value.lower() in {"bearer", "basic"}:
                following += 1
            if following < len(tokens):
                spans.append(_value_span(text, tokens, following))
                if tokens[following].content_start != tokens[following].start:
                    _remember(tokens[following].value, credentials)
    spans.extend(match.span() for match in _SIGNED_URL.finditer(text))
    spans.extend(match.span(2) for match in _SIGNED_QUERY.finditer(text))
    spans.extend(match.span() for match in _BASE64.finditer(text))
    for start, end in spans:
        _remember(text[start:end], credentials)
    return spans


def _remember(text: str, credentials: set[str]) -> None:
    """Recover raw leaves even when the SDK has represented a deep subtree."""
    pending = [text]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        decoded = _literal(current)
        if not current or decoded == MARKER or current in seen:
            continue
        seen.add(current)
        credentials.add(current)
        credentials.update((repr(current)[1:-1], json.dumps(current)[1:-1]))
        if decoded != current:
            pending.append(decoded)
        if current.startswith(("{", "[", "(")):
            tokens = _tokens(current)
            pending.extend(
                token.value
                for index, token in enumerate(tokens)
                if token.content_start != token.start
                and (index + 1 == len(tokens) or tokens[index + 1].value != ":")
            )


def _children(
    value: object, hidden: bool, fields: frozenset[str] | None
) -> list[tuple[object, bool]]:
    match value:
        case Mapping():
            return [
                child
                for key, item in value.items()
                for child in ((key, hidden), (item, hidden or _sensitive(key, fields)))
            ]
        case [key, item] if _sensitive(key, fields):
            return [(key, hidden), (item, True)]
        case list() | tuple():
            return [(item, hidden) for item in value]
        case set() | frozenset():
            return [(item, hidden) for item in value]
        case _:
            return []


def _rebuild(value: object, items: list[object]) -> object:
    match value:
        case Mapping():
            return dict(zip(items[::2], items[1::2], strict=True))
        case list():
            return items
        case tuple():
            return tuple(items)
        case set():
            return set(items)
        case frozenset():
            return frozenset(items)
        case _:
            return value


def _walk(
    value: object,
    visit: Callable[[object, bool], object],
    fields: frozenset[str] | None,
) -> object:
    """Walk without a recursion/depth limit; cycles become a safe edge marker."""
    pending: list[tuple[object, bool, int]] = [(value, False, -1)]
    results: list[object] = []
    active: set[int] = set()
    while pending:
        current, hidden, count = pending.pop()
        if count > 0:
            items = results[-count:]
            del results[-count:]
            active.remove(id(current))
            results.append(visit(_rebuild(current, items), hidden))
            continue
        if id(current) in active:
            results.append(MARKER)
            continue
        children = _children(current, hidden, fields)
        if children:
            active.add(id(current))
            pending.append((current, hidden, len(children)))
            pending.extend((item, secret, -1) for item, secret in reversed(children))
        else:
            results.append(visit(current, hidden))
    return results[0]


def _union(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping and adjacent ranges so no part of any range survives."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


class _KnownValues:
    """Aho-Corasick index, built once for all strings in a discovered record."""

    def __init__(self, values: set[str]) -> None:
        if sum(map(len, values)) > _MAX_SCAN_CHARACTERS:
            raise RedactionBudgetError(_SCAN_BUDGET_ERROR)
        self.edges: list[dict[str, int]] = [{}]
        self.failure = [0]
        self.lengths = [0]
        for value in values:
            state = 0
            for character in value:
                if character not in self.edges[state]:
                    self.edges[state][character] = len(self.edges)
                    self.edges.append({})
                    self.failure.append(0)
                    self.lengths.append(0)
                state = self.edges[state][character]
            self.lengths[state] = len(value)
        pending = deque(self.edges[0].values())
        while pending:
            state = pending.popleft()
            for character, following in self.edges[state].items():
                pending.append(following)
                fallback = self.failure[state]
                while fallback and character not in self.edges[fallback]:
                    fallback = self.failure[fallback]
                self.failure[following] = self.edges[fallback].get(character, 0)
                # All matches ending here are contained in the longest one.
                self.lengths[following] = max(
                    self.lengths[following], self.lengths[self.failure[following]]
                )

    def spans(self, text: str) -> list[tuple[int, int]]:
        """Cover every overlapping match without storing each occurrence."""
        merged: list[tuple[int, int]] = []
        state = 0
        for end, character in enumerate(text, 1):
            while state and character not in self.edges[state]:
                state = self.failure[state]
            state = self.edges[state].get(character, 0)
            length = self.lengths[state]
            if length:
                start = end - length
                # Ends increase, but a longer match can enclose earlier ranges.
                while merged and start <= merged[-1][1]:
                    start = min(start, merged.pop()[0])
                merged.append((start, end))
        return merged


def discover_structure(
    value: object,
    credentials: set[str],
    *,
    fields: frozenset[str] | None = frozenset(),
    urls: Callable[[str], tuple[str, set[str]]] | None = None,
) -> dict[str, tuple[str, list[tuple[int, int]]]]:
    """Learn raw leaves and text ranges without rendering opaque objects."""
    plans: dict[str, tuple[str, list[tuple[int, int]]]] = {}
    characters = 0

    def discover(item: object, hidden: bool) -> object:
        nonlocal characters
        if isinstance(item, (str, bytes)):
            characters += len(item)
            if characters > _MAX_SCAN_CHARACTERS:
                raise RedactionBudgetError(_SCAN_BUDGET_ERROR)
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
    return plans


def redact_structure(
    value: object,
    credentials: set[str],
    *,
    fields: frozenset[str] | None = frozenset(),
    urls: Callable[[str], tuple[str, set[str]]] | None = None,
) -> object:
    """Discover first, replace second, across the entire record and all its keys."""
    plans = discover_structure(value, credentials, fields=fields, urls=urls)
    # Include the marker itself, without exempting marker-prefixed credentials.
    known = _KnownValues((credentials - {""}) | {MARKER})

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
        spans = [*spans, *known.spans(redacted)]
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


def scrub_text(value: str, credentials: set[str]) -> str:
    """Use the same discovery and replacement rules for rendered diagnostics."""
    try:
        return str(redact_structure(value, credentials))
    except RedactionBudgetError:
        return MARKER


def scrub_provider_text(value: str) -> str:
    """Scrub a rendered diagnostic without any request or active-secret context."""
    return scrub_text(value, set())


def render_structure(value: object) -> str:
    """Render a deep diagnostic with the stack walker, not recursive repr."""

    def render(item: object, _hidden: bool) -> object:
        match item:
            case Mapping():
                return (
                    "{"
                    + ", ".join(f"{key}: {part}" for key, part in item.items())
                    + "}"
                )
            case list():
                return "[" + ", ".join(str(part) for part in item) + "]"
            case tuple():
                content = ", ".join(str(part) for part in item)
                return "(" + content + ("," if len(item) == 1 else "") + ")"
            case set() | frozenset():
                return "{" + ", ".join(sorted(str(part) for part in item)) + "}"
            case _:
                return repr(item)

    return str(_walk(value, render, frozenset()))
