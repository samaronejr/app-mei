"""Body boundaries, final status, and field lists on the real transport."""

import pytest

from apps.integrations.call_models import CallState
from apps.integrations.focus.ciencia_wire import _registration_result
from apps.integrations.transport_types import ProviderResponse, TransportFailure
from apps.integrations.transport_wire import XML_LIMIT
from tests.fakes.focus import FakeFocus
from tests.fakes.provider import Reply
from tests.integrations.test_transport_representation import (
    REFUSED,
    REGISTERED,
    wire_outcome,
)

HTTP11 = b"HTTP/1.1 200 OK\r\n"
CLOSE = b"Connection: close\r\n\r\n"
CHUNKED = b"Transfer-Encoding: chunked\r\n"
EARLY_HINTS = b"HTTP/1.1 103 Early Hints\r\n\r\n"


def chunk(body: bytes, trailers: bytes = b"") -> bytes:
    return f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n" + trailers + b"\r\n"


def framed(body: bytes, framing: str) -> bytes:
    if framing == "close":
        return CLOSE + body
    if framing == "chunked":
        return CHUNKED + b"\r\n" + chunk(body)
    assert framing in {"length", "short-length"}
    length = len(body) + (framing == "short-length")
    return f"Content-Length: {length}\r\n\r\n".encode() + body


def expect_body(
    outcome: ProviderResponse | TransportFailure | ValueError, body: bytes, valid: bool
) -> None:
    if valid:
        assert isinstance(outcome, ProviderResponse), outcome
        assert (outcome.status, outcome.body) == (200, body)
        state = CallState.FAILED if body == REFUSED else CallState.SUCCESS
        assert _registration_result(outcome).state == state
    else:
        assert isinstance(outcome, TransportFailure), outcome
        assert outcome.code
        assert _registration_result(outcome).state == CallState.UNCERTAIN


@pytest.mark.parametrize("body", [REGISTERED, REFUSED], ids=["registered", "refused"])
@pytest.mark.parametrize("framing", ["length", "close", "chunked"])
@pytest.mark.parametrize("version", ["1.0", "1.1"])
def test_body_framing_uses_the_response_version(
    version: str, framing: str, body: bytes
) -> None:
    reply = f"HTTP/{version} 200 OK\r\n".encode() + framed(body, framing)
    with FakeFocus((Reply(raw_response=reply),)) as peer:
        expect_body(wire_outcome(peer), body, version != "1.0" or framing != "chunked")
        assert len(peer.requests) == 1


@pytest.mark.parametrize(
    ("fields", "valid"),
    [
        (b"Transfer-Encoding: chunked,\r\n", True),
        (b"Transfer-Encoding: ,  ChUnKeD,\t,\r\n", True),
        (b"Transfer-Encoding: ,\r\nTransfer-Encoding: chunked\r\n", True),
        # No coding: the chunk bytes are the literal close-delimited body.
        (b"Transfer-Encoding: \t, \r\n", None),
        (b"Transfer-Encoding: chunked, ,chunked\r\n", False),
        (b"Transfer-Encoding: gzip, ,chunked\r\n", False),
        (b"Transfer-Encoding: chunked,\r\nContent-Length: 55\r\n", False),
        (b"Transfer-Encoding: " + b"," * 17 + b"chunked\r\n", False),
    ],
    ids=[
        "trailing-empty",
        "both-edges",
        "split-fields",
        "all-empty",
        "repeated",
        "other-coding",
        "mixed",
        "too-many-empty",
    ],
)
def test_transfer_coding_lists_reach_the_same_chunk_reader(
    fields: bytes, valid: bool | None
) -> None:
    wire = HTTP11 + fields + b"\r\n" + chunk(REFUSED)
    with FakeFocus((Reply(raw_response=wire),)) as peer:
        outcome = wire_outcome(peer)
        if valid is None:
            assert isinstance(outcome, ProviderResponse), outcome
            assert outcome.body == chunk(REFUSED)
            assert _registration_result(outcome).state == CallState.UNCERTAIN
        else:
            expect_body(outcome, REFUSED, valid)
        assert len(peer.requests) == 1


@pytest.mark.parametrize("body", [REGISTERED, REFUSED], ids=["registered", "refused"])
@pytest.mark.parametrize(
    ("head", "valid"),
    [
        (b"HTTP/1.0 200 OK\r\n", True),
        (b"HTTP/1.1 200 OK\r\n", True),
        (b"HTTP/1.1 200\r\n", True),
        (b"HTTP/1.1 103 Early Hints\r\n\r\nHTTP/1.0 200 OK\r\n", True),
        (b"HTTP/0.9 200 OK\r\n", False),
        (b"HTTP/2 200 OK\r\n", False),
        (b"HTTP/2.0 200 OK\r\n", False),
        (b"HTTP/1.2 200 OK\r\n", False),
        (b"HTTP/1.10 200 OK\r\n", False),
        (b"http/1.1 200 OK\r\n", False),
        (b"HTTP/1. 200 OK\r\n", False),
        (b"HTTP/1 200 OK\r\n", False),
        (b"HTTP/ 200 OK\r\n", False),
        (b"200 OK\r\n", False),
        (b"HTTP/1.1  200 OK\r\n", False),
        (b"HTTP/0.9 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\n", False),
    ],
    ids=[
        "http-1.0",
        "http-1.1",
        "no-reason",
        "interim-then-1.0",
        "http-0.9",
        "http-2",
        "http-2.0",
        "http-1.2",
        "two-digit-minor",
        "lowercase-name",
        "missing-minor-digit",
        "missing-minor",
        "missing-version",
        "no-version-token",
        "double-space",
        "interim-0.9",
    ],
)
def test_only_http_1_0_and_1_1_status_lines_settle(
    head: bytes, valid: bool, body: bytes
) -> None:
    """RFC 9112 §§2.3, 4: exactly HTTP/1.0 or HTTP/1.1 can establish a status."""
    with FakeFocus((Reply(raw_response=head + framed(body, "length")),)) as peer:
        outcome = wire_outcome(peer)
        expect_body(outcome, body, valid)
        if not valid:
            # The head reader itself refuses the line; the library is not relied on.
            assert isinstance(outcome, TransportFailure)
            assert outcome.code == "resposta_invalida"
        assert len(peer.requests) == 1


@pytest.mark.parametrize("body", [REGISTERED, REFUSED], ids=["registered", "refused"])
@pytest.mark.parametrize(
    ("wire", "valid"),
    [
        (HTTP11 + b"Transfer-Encoding: ,\r\n" + CLOSE, True),
        (HTTP11 + b"Transfer-Encoding:\r\n" + CLOSE, True),
        (
            HTTP11 + b"Transfer-Encoding: \t, ,\r\nTransfer-Encoding: ,\r\n" + CLOSE,
            True,
        ),
        # The field is still present: with a length it is conflicting framing.
        (HTTP11 + b"Transfer-Encoding: ,\r\nContent-Length: {length}\r\n\r\n", False),
        (HTTP11 + b"Content-Encoding: ,\r\nTransfer-Encoding: ,\r\n" + CLOSE, True),
        (HTTP11 + b"Transfer-Encoding: " + b"," * 17 + b"\r\n" + CLOSE, False),
        (b"HTTP/1.0 200 OK\r\nTransfer-Encoding: ,\r\n" + CLOSE, False),
        (HTTP11 + b"Content-Length: ,\r\n" + CLOSE, False),
    ],
    ids=[
        "coding-empty-list",
        "coding-empty-value",
        "coding-empty-fields",
        "coding-empty-then-length",
        "both-coding-lists-empty",
        "coding-too-many-empty",
        "http10-empty-coding-field",
        "length-is-not-a-list",
    ],
)
def test_all_empty_coding_lists_are_absent_for_framing(
    wire: bytes, valid: bool, body: bytes
) -> None:
    """An all-empty `#transfer-coding` list applies no coding (RFC 9112 §6.1).

    Only the choice of codings uses the members: the field itself stays present,
    so it still conflicts with a length and is faulty on HTTP/1.0. Content-Length
    is `1*DIGIT`, not a list, so an all-empty length is invalid.
    """
    reply = wire.replace(b"{length}", str(len(body)).encode()) + body
    with FakeFocus((Reply(raw_response=reply),)) as peer:
        expect_body(wire_outcome(peer), body, valid)
        assert len(peer.requests) == 1


@pytest.mark.parametrize("body", [REGISTERED, REFUSED], ids=["registered", "refused"])
@pytest.mark.parametrize(
    "wire",
    [
        HTTP11 + b"Transfer-Encoding: ,\r\nContent-Length: {length}\r\n\r\n",
        HTTP11 + b"Content-Length: {length}\r\nTransfer-Encoding:\r\n\r\n",
        HTTP11
        + b"Transfer-Encoding: ,\r\nTransfer-Encoding: \t\r\n"
        + b"Content-Length: {length}\r\n\r\n",
        b"HTTP/1.1 103 Early Hints\r\nTransfer-Encoding: ,\r\n\r\n"
        + HTTP11
        + b"Content-Length: {length}\r\n\r\n",
        b"HTTP/1.1 100 Continue\r\nTransfer-Encoding:\r\n\r\n"
        + HTTP11
        + b"Content-Length: {length}\r\n\r\n",
        b"HTTP/1.1 204 No Content\r\nTransfer-Encoding: ,\r\n\r\n",
    ],
    ids=[
        "empty-coding-then-length",
        "length-then-empty-value",
        "empty-coding-fields-then-length",
        "interim-empty-coding",
        "continue-empty-value",
        "no-content-empty-coding",
    ],
)
def test_transfer_encoding_presence_rules_ignore_its_members(
    wire: bytes, body: bytes
) -> None:
    """Field presence decides conflicts and forbidden statuses (RFC 9112 §§6.1-6.3)."""
    reply = wire.replace(b"{length}", str(len(body)).encode())
    reply += b"" if b" 204 " in wire else body
    with FakeFocus((Reply(raw_response=reply),)) as peer:
        outcome = wire_outcome(peer)
        expect_body(outcome, body, valid=False)
        assert isinstance(outcome, TransportFailure)
        assert outcome.code == "resposta_invalida"
        assert len(peer.requests) == 1


def test_not_modified_may_carry_transfer_encoding() -> None:
    """RFC 9112 §6.1: a 304 may name the coding it would have applied."""
    wire = b"HTTP/1.1 304 Not Modified\r\nTransfer-Encoding: chunked\r\n\r\n"
    with FakeFocus((Reply(raw_response=wire),)) as peer:
        outcome = wire_outcome(peer)
        assert isinstance(outcome, ProviderResponse), outcome
        assert (outcome.status, outcome.body) == (304, b"")
        assert len(peer.requests) == 1


OVER_CAP = str(XML_LIMIT + 1).encode()
NOT_MODIFIED = b"HTTP/1.1 304 Not Modified\r\n"
GZIP_ETAG = b'Content-Encoding: gzip\r\nETag: "v1"\r\n\r\n'


@pytest.mark.parametrize(
    ("wire", "status"),
    [
        (NOT_MODIFIED + CHUNKED + GZIP_ETAG, 304),
        (
            b"HTTP/1.1 304 Not Modified\r\nContent-Length: " + OVER_CAP + b"\r\n\r\n",
            304,
        ),
        (NOT_MODIFIED + GZIP_ETAG, 304),
        (b"HTTP/1.1 304 Not Modified\r\nContent-Length: 98\r\n\r\n", 304),
        (b'HTTP/1.1 304 Not Modified\r\nETag: "v1"\r\n\r\n' + REGISTERED, 304),
        (b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n", 204),
        (b"HTTP/1.1 204 No Content\r\nContent-Encoding: gzip\r\n\r\n", 204),
        (b"HTTP/1.1 204 No Content\r\nContent-Length: " + OVER_CAP + b"\r\n\r\n", 204),
        (
            b"HTTP/1.1 304 Not Modified\r\nContent-Length: " + b"9" * 21 + b"\r\n\r\n",
            None,
        ),
        (b"HTTP/1.1 304 Not Modified\r\nContent-Length: 9a\r\n\r\n", None),
        (b"HTTP/1.1 304 Not Modified\r\nContent-Length: 5, 6\r\n\r\n", None),
        (NOT_MODIFIED + CHUNKED + b"Content-Length: 98\r\n\r\n", None),
        (b"HTTP/1.1 200 OK\r\nContent-Length: " + OVER_CAP + b"\r\n\r\n", None),
    ],
    ids=[
        "not-modified-coding-metadata",
        "not-modified-representation-length",
        "not-modified-gzip-metadata",
        "not-modified-small-length",
        "not-modified-bytes-after-head",
        "no-content-zero-length",
        "no-content-gzip",
        "no-content-representation-length",
        "not-modified-digit-bound",
        "not-modified-malformed-length",
        "not-modified-disagreeing-lengths",
        "not-modified-length-and-coding",
        "ok-over-cap-control",
    ],
)
def test_bodyless_responses_ignore_representation_fields(
    wire: bytes, status: int | None
) -> None:
    """RFC 9112 §6.3(1), RFC 9110 §§8.6, 15.4.5: no body to frame, decode or cap.

    Raw-field syntax, digit bounds, agreement and the sender's length/coding
    conflict still apply; bytes after the head are never read as a body.
    """
    with FakeFocus((Reply(raw_response=wire),)) as peer:
        outcome = wire_outcome(peer)
        if status is None:
            assert isinstance(outcome, TransportFailure), outcome
        else:
            assert isinstance(outcome, ProviderResponse), outcome
            assert (outcome.status, outcome.body) == (status, b"")
            if b"ETag" in wire:
                assert dict(outcome.headers)["etag"] == '"v1"'
        assert len(peer.requests) == 1


@pytest.mark.parametrize("body", [REGISTERED, REFUSED], ids=["registered", "refused"])
@pytest.mark.parametrize(
    ("fields", "valid"),
    [
        (b"", True),
        (b"Content-Encoding: ,\r\n", True),
        (b"Content-Encoding: identity, \r\n", True),
        (b"Content-Encoding:\r\n", True),
        (b"Content-Encoding: \t, IdEnTiTy ,\t,\r\n", True),
        (b"Content-Encoding: IDENTITY\r\n", True),
        (b"Content-Encoding: identity\r\nContent-Encoding: ,\r\n", True),
        (b"Content-Encoding: identity, identity\r\n", True),
        (b"Content-Encoding: " + b"," * 16 + b"identity\r\n", True),
        (b"Content-Encoding: x-unknown\r\n", False),
        (b"Content-Encoding: identity, x-unknown\r\n", False),
        (b"Content-Encoding: gzip, \r\n", False),
        (b"Content-Encoding: identity\r\nContent-Encoding: br\r\n", False),
        (b'Content-Encoding: "identity"\r\n', False),
        (b"Content-Encoding: identity identity\r\n", False),
        (b"Content-Encoding: " + b"," * 17 + b"identity\r\n", False),
    ],
    ids=[
        "absent",
        "empty-list",
        "identity-trailing-empty",
        "empty-value",
        "mixed-case-edges",
        "upper-identity",
        "split-fields",
        "repeated-identity",
        "empty-members-at-bound",
        "unknown",
        "identity-then-unknown",
        "gzip-trailing-empty",
        "split-unknown",
        "quoted",
        "space-separated",
        "too-many-empty",
    ],
)
def test_content_coding_lists_follow_the_identity_rule(
    fields: bytes, valid: bool, body: bytes
) -> None:
    """RFC 9110 §8.4: only an empty list or identity members leave the body as sent."""
    wire = HTTP11 + fields + framed(body, "length")
    with FakeFocus((Reply(raw_response=wire),)) as peer:
        expect_body(wire_outcome(peer), body, valid)
        assert len(peer.requests) == 1


@pytest.mark.parametrize("framing", ["length", "chunked", "close"])
@pytest.mark.parametrize(
    "fields",
    [
        b"",
        b"Connection: close\r\n",
        b"Connection: , Close ,\t,\r\n",
        b"Connection: keep-alive\r\n",
        b"Connection: Keep-Alive, ,close\r\nConnection: x-option\r\n",
    ],
    ids=["absent", "close", "close-with-empties", "keep-alive", "mixed-split"],
)
def test_connection_options_never_change_the_settled_body(
    fields: bytes, framing: str
) -> None:
    """Only the library's reuse flag reads Connection; framing alone ends the body."""
    ending = b"\r\n" + REFUSED if framing == "close" else framed(REFUSED, framing)
    with FakeFocus((Reply(raw_response=HTTP11 + fields + ending),)) as peer:
        expect_body(wire_outcome(peer), REFUSED, valid=True)
        assert len(peer.requests) == 1


@pytest.mark.parametrize(
    ("statuses", "final", "valid"),
    [
        ((100,), True, True),
        ((102,), True, True),
        ((103,), True, True),
        ((103, 100, 102, 199, 103), True, True),
        ((100, 103), False, False),
        ((101,), False, False),
        ((103, 101), True, False),
    ],
    ids=[
        "continue",
        "processing",
        "early-hints",
        "sequence",
        "no-final",
        "switch",
        "switch-before-apparent-final",
    ],
)
def test_only_the_final_response_can_settle_the_exchange(
    statuses: tuple[int, ...], final: bool, valid: bool
) -> None:
    interims = b"".join(f"HTTP/1.1 {s} Interim\r\n\r\n".encode() for s in statuses)
    ending = HTTP11 + framed(REGISTERED, "length") if final else b""
    with FakeFocus((Reply(raw_response=interims + ending),)) as peer:
        expect_body(wire_outcome(peer), REGISTERED, valid)
        assert len(peer.requests) == 1


@pytest.mark.parametrize(
    "header",
    [b"Content-Length: 0", b"Transfer-Encoding: chunked", b"X-Bad : value"],
    ids=["interim-length", "interim-coding", "malformed-interim-field"],
)
def test_informational_head_must_be_valid_before_it_is_skipped(header: bytes) -> None:
    wire = (
        b"HTTP/1.1 103 Early Hints\r\n"
        + header
        + b"\r\n\r\n"
        + HTTP11
        + framed(REGISTERED, "length")
    )
    with FakeFocus((Reply(raw_response=wire),)) as peer:
        expect_body(wire_outcome(peer), REGISTERED, valid=False)
        assert len(peer.requests) == 1
