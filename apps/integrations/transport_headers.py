"""Bound and normalize provider response fields before HTTP library framing."""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Final

from apps.integrations.transport_types import TransportFailure

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import BinaryIO

FRAMING_LIMIT: Final = 65536
TOKEN: Final = rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
_TEXT: Final = rb"[\t\x20-\x7e\x80-\xff]*"
_FIELD_LINE: Final = re.compile(rb"(" + TOKEN + rb"):(" + _TEXT + rb")\r\n")
_CONTINUATION: Final = re.compile(rb"[ \t]" + _TEXT + rb"\r\n")
_STATUS_LINE: Final = re.compile(
    # RFC 9112 §§2.3, 4: case-sensitive HTTP-name and one digit each side;
    # only 1.0 and 1.1 have this status-line and field grammar.
    rb"HTTP/(?P<version>1\.[01]) (?P<status>[1-9][0-9]{2})(?: " + _TEXT + rb")?\r\n"
)
_OWS: Final = b" \t"
_MAX_FIELD_LINES: Final = 100  # Includes the terminating CRLF, as in http.client.
_MAX_LENGTH_DIGITS: Final = 20
_MAX_EMPTY_MEMBERS: Final = 16
_INVALID: Final = "resposta_invalida"
_TOO_LARGE: Final = "resposta_excede_limite"


def read_fields(stream: BinaryIO, status: int) -> list[tuple[bytes, bytes]]:
    """Read a bounded header or trailer section through its terminating CRLF."""
    fields: list[tuple[bytes, bytes]] = []
    remaining = FRAMING_LIMIT
    for _ in range(_MAX_FIELD_LINES):
        line = stream.readline(remaining + 1)
        remaining -= len(line)
        if remaining < 0:
            raise TransportFailure(_TOO_LARGE, status)
        if line == b"\r\n":
            return fields
        if match := _FIELD_LINE.fullmatch(line):
            fields.append((match[1].lower(), match[2]))
        elif fields and _CONTINUATION.fullmatch(line):
            # RFC 9112 §5.2: unfold into SP before interpreting the value.
            name, previous = fields[-1]
            fields[-1] = (name, previous.rstrip(_OWS) + b" " + line[:-2].lstrip(_OWS))
        else:
            raise TransportFailure(_INVALID, status)
    raise TransportFailure(_TOO_LARGE, status)


def _members(values: list[bytes], status: int) -> Iterator[bytes]:
    """Ignore a bounded number of empty list elements (RFC 9110 §5.6.1.2)."""
    empty = 0
    for value in values:
        for member in value.split(b","):
            if item := member.strip(_OWS):
                yield item
            else:
                empty += 1
                if empty > _MAX_EMPTY_MEMBERS:
                    raise TransportFailure(_TOO_LARGE, status)


def _codings(values: list[bytes], status: int) -> list[bytes]:
    """Transfer and content codings are case-insensitive (RFC 9110 §8.4.1)."""
    return [item.lower() for item in _members(values, status)]


def _content_length(
    values: list[bytes], maximum: int | None, status: int
) -> bytes | None:
    """Return one bounded decimal string for all fields/list members, or reject.

    `maximum` caps a body about to be received; it is None when the response
    has no body and the field only describes the selected representation.
    """
    bound = str(maximum).encode("ascii") if maximum is not None else None
    canonical = None
    for digits in _members(values, status):
        if not digits.isdigit():  # bytes: nonempty ASCII digits only
            raise TransportFailure(_INVALID, status)
        if len(digits) > _MAX_LENGTH_DIGITS:
            raise TransportFailure(_TOO_LARGE, status)
        number = digits.lstrip(b"0") or b"0"
        if bound is not None and (len(number), number) > (len(bound), bound):
            raise TransportFailure(_TOO_LARGE, status)
        if canonical is not None and number != canonical:
            raise TransportFailure(_INVALID, status)
        canonical = number
    if values and canonical is None:
        raise TransportFailure(_INVALID, status)
    return canonical


def _header_section(
    stream: BinaryIO, version: bytes, status: int, maximum: int
) -> bytes:
    fields = read_fields(stream, status)
    # RFC 9112 §6.3(1): 1xx, 204 and 304 end at the header terminator whatever their
    # fields say. Their Content-Length, Transfer-Encoding and Content-Encoding
    # describe the selected representation, never bytes to frame, decode or cap.
    bodyless = status < HTTPStatus.OK or status in {
        HTTPStatus.NO_CONTENT,
        HTTPStatus.NOT_MODIFIED,
    }
    length = _content_length(
        [value for name, value in fields if name == b"content-length"],
        None if bodyless else maximum,
        status,
    )
    fields_te = [value for name, value in fields if name == b"transfer-encoding"]
    if fields_te and version < b"1.1":
        # RFC 9112 §6.1: any Transfer-Encoding field in HTTP/1.0 is faulty framing.
        raise TransportFailure(_INVALID, status)
    if status < HTTPStatus.OK and length is not None:
        raise TransportFailure(_INVALID, status)
    # Presence rules fire on the field itself, even when its list is empty
    # (RFC 9112 §§6.1-6.3): forbidden on 1xx and 204, conflicting with a length.
    if fields_te and (
        status < HTTPStatus.OK or status == HTTPStatus.NO_CONTENT or length is not None
    ):
        raise TransportFailure(_INVALID, status)
    # Only the codings to apply use the members: an all-empty list applies none,
    # and a bodyless response has no content to apply them to.
    transfer = [] if bodyless else _codings(fields_te, status)
    if transfer and transfer != [b"chunked"]:
        msg = "codificacao_nao_permitida"
        raise TransportFailure(msg, status)
    # RFC 9110 §8.4: an empty list or identity leaves the content as sent. The
    # caller decides on what remains, after redirects have been handled.
    content = [value for name, value in fields if name == b"content-encoding"]
    codings = (
        []
        if bodyless
        else [coding for coding in _codings(content, status) if coding != b"identity"]
    )
    lines = [
        name + b":" + value.strip(_OWS) + b"\r\n"
        for name, value in fields
        if name not in {b"content-length", b"transfer-encoding", b"content-encoding"}
    ]
    if length is not None and not bodyless:
        lines.append(b"Content-Length:" + length + b"\r\n")
    if transfer:
        lines.append(b"Transfer-Encoding:chunked\r\n")
    if codings:
        lines.append(b"Content-Encoding:" + b",".join(codings) + b"\r\n")
    return b"".join(lines) + b"\r\n"


def read_response_head(stream: BinaryIO, maximum: int) -> bytes:
    """Read only the head, leaving the body in the original buffered stream."""
    while True:
        line = stream.readline(FRAMING_LIMIT + 1)
        if not line:
            msg = "conexao_interrompida"
            raise TransportFailure(msg)
        match = _STATUS_LINE.fullmatch(line) if len(line) <= FRAMING_LIMIT else None
        if match is None:
            raise TransportFailure(_INVALID)
        status = int(match["status"])  # Exactly three status digits, not a length.
        section = _header_section(stream, match["version"], status, maximum)
        if status == HTTPStatus.SWITCHING_PROTOCOLS:
            # No provider operation requested an upgrade.
            raise TransportFailure(_INVALID, status)
        if status >= HTTPStatus.OK:
            return line + section
