"""A terminal event result requires a complete, readable HTTP representation."""

import pytest

from apps.integrations.call_models import CallState
from apps.integrations.focus.ciencia_wire import _registration_result
from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import (
    ProviderRequest,
    ProviderResponse,
    TransportFailure,
)
from apps.integrations.transport_wire import XML_LIMIT
from tests.fakes.focus import FakeFocus
from tests.fakes.provider import Reply, permitted_send

REGISTERED = (
    b'{"status":"evento_registrado","status_sefaz":"135",'
    b'"tipo":"ciencia","protocolo":"891170005150285"}'
)
REFUSED = b'{"status":"erro","status_sefaz":"596","tipo":"ciencia"}'


def wire_outcome(
    peer: FakeFocus, ca_file: str = ""
) -> ProviderResponse | TransportFailure | ValueError:
    try:
        return permitted_send(
            ProviderTransport("focus", "fake", peer.origin, ca_file),
            "FC-NFE",
            ProviderRequest(),
        )
    except (TransportFailure, ValueError) as error:
        return error


@pytest.mark.parametrize("body", [REGISTERED, REFUSED], ids=["registered", "refused"])
@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [
        (b"{size}\r\n", b"\r\n0\r\n"),
        (b"{size}\r\n", b"\r\n0"),
        (b"{size}\n", b"\r\n0\r\n\r\n"),
        (b"{size}\r\n", b"\r\n0\r\n\n"),
        (b"{size}\r\n", b"\r\n0\r\nX-Note: value\r\n"),
        (b"{size}\r\n", b"\r\n0\r\nX-Note: value"),
        (b"{size}\r\n", b"\r\n0\r\nnot-a-field\r\n\r\n"),
        (b"{size}\r\n", b"\r\n0\r\n X-Note: value\r\n\r\n"),
        (b"{size}\r\n", b"\r\n0\r\nX-Note: \x00\r\n\r\n"),
        (b"{size}\r\n", b"\r\n0\r\nX-Note: " + b"x" * 65536 + b"\r\n\r\n"),
        (b"{size};\r\n", b"\r\n0\r\n\r\n"),
        (b'{size};note="unfinished\r\n', b"\r\n0\r\n\r\n"),
        (b"{size};bad name=value\r\n", b"\r\n0\r\n\r\n"),
        (b"{size};note=\r\n", b"\r\n0\r\n\r\n"),
        (b"{size};note=\x00\r\n", b"\r\n0\r\n\r\n"),
        (b"+{size}\r\n", b"\r\n0\r\n\r\n"),
        (b" {size}\r\n", b"\r\n0\r\n\r\n"),
        (b"0x{size}\r\n", b"\r\n0\r\n\r\n"),
        (b"GG\r\n", b"\r\n0\r\n\r\n"),
        (b"f" * 4301 + b"\r\n", b"\r\n0\r\n\r\n"),
        (b"0" * 65536 + b"{size}\r\n", b"\r\n0\r\n\r\n"),
        (b"{size}\r\n", b"XX0\r\n\r\n"),
        (b"{size}\r\n", b"\n0\r\n\r\n"),
    ],
    ids=[
        "reviewer-missing-final-empty-line",
        "unterminated-zero-chunk",
        "bare-lf-size",
        "bare-lf-trailer-terminator",
        "trailer-eof-after-field",
        "trailer-eof-inside-field",
        "trailer-without-colon",
        "folded-trailer",
        "control-in-trailer",
        "oversized-trailer",
        "empty-extension",
        "unterminated-quoted-extension",
        "invalid-extension-name",
        "missing-extension-value",
        "control-in-extension",
        "signed-size",
        "whitespace-size",
        "prefixed-size",
        "non-hex-size",
        "oversized-size",
        "oversized-size-line",
        "wrong-data-crlf",
        "missing-data-crlf",
    ],
)
def test_malformed_chunk_framing_never_delivers_a_body(
    body: bytes, prefix: bytes, suffix: bytes
) -> None:
    reply = Reply(
        body=prefix.replace(b"{size}", f"{len(body):x}".encode()) + body + suffix,
        headers=(("Transfer-Encoding", "chunked"),),
        declared_length=False,
    )
    with FakeFocus((reply,)) as peer:
        outcome = wire_outcome(peer)
        assert isinstance(outcome, TransportFailure), outcome
        assert outcome.code in {
            "resposta_invalida",
            "resposta_excede_limite",
            "conexao_interrompida",
        }
        assert len(peer.requests) == 1


@pytest.mark.parametrize("body", [REGISTERED, REFUSED], ids=["registered", "refused"])
@pytest.mark.parametrize("coding", ["chunked ", "chunked\t", " chunked "])
def test_claimed_chunk_coding_cannot_deliver_unframed_json(
    body: bytes, coding: str
) -> None:
    reply = Reply(
        body=body, headers=(("Transfer-Encoding", coding),), declared_length=False
    )
    with FakeFocus((reply,)) as peer:
        outcome = wire_outcome(peer)
        assert isinstance(outcome, TransportFailure), outcome
        assert len(peer.requests) == 1


@pytest.mark.parametrize(
    ("body", "state"),
    [(REGISTERED, CallState.SUCCESS), (REFUSED, CallState.FAILED)],
    ids=["registered", "refused"],
)
@pytest.mark.parametrize(
    ("extension", "ending"),
    [
        (b"", b"0\r\n\r\n"),
        (b";flag", b"0;done\r\n\r\n"),
        (b";name=value", b"000\r\nX-Note: ignored\r\n\r\n"),
        (b';note="a; b\\"c";flag', b"0\r\nX-A: one\r\nX-B: two\r\n\r\n"),
        (b" \t; name = value", b"0\r\n\r\n"),
    ],
    ids=["plain", "flag-extension", "token-and-trailer", "quoted-and-trailers", "bws"],
)
def test_complete_chunks_extensions_and_trailers_preserve_terminal_controls(
    body: bytes, state: str, extension: bytes, ending: bytes
) -> None:
    reply = Reply(
        body=f"{len(body):x}".encode() + extension + b"\r\n" + body + b"\r\n" + ending,
        headers=(("Transfer-Encoding", "chunked"),),
        declared_length=False,
    )
    with FakeFocus((reply,)) as peer:
        response = permitted_send(
            ProviderTransport("focus", "fake", peer.origin),
            "FC-NFE",
            ProviderRequest(),
        )
        assert response.body == body
        assert _registration_result(response).state == state
        assert len(peer.requests) == 1


@pytest.mark.parametrize(
    "headers",
    [
        (("Content-Length", "9" * 4301),),
        (("Content-Length", "0" * 4301),),
        (("Content-Length", str(XML_LIMIT + 1)),),
        (("Content-Length", ""),),
        (("Content-Length", "-1"),),
        (("Content-Length", "+2"),),
        (("Content-Length", "2.0"),),
        (("Content-Length", "2 0"),),
        (("Content-Length", "\xb2"),),
        (("Content-Length", "2, 3"),),
        (("Content-Length", "2"), ("Content-Length", "3")),
    ],
    ids=[
        "reviewer-4301-nines",
        "unbounded-leading-zeroes",
        "body-limit-plus-one",
        "empty",
        "negative",
        "signed",
        "fractional",
        "internal-whitespace",
        "non-ascii-digit",
        "conflicting-combined-values",
        "conflicting-duplicate-fields",
    ],
)
def test_invalid_declared_length_is_a_typed_wire_failure(
    headers: tuple[tuple[str, str], ...],
) -> None:
    with FakeFocus((Reply(body=b"", headers=headers, declared_length=False),)) as peer:
        outcome = wire_outcome(peer)
        assert isinstance(outcome, TransportFailure), outcome
        assert outcome.code in {
            "resposta_invalida",
            "resposta_excede_limite",
            "conexao_interrompida",
        }
        assert len(peer.requests) == 1


@pytest.mark.parametrize("padding", ["", "000"])
def test_bounded_decimal_lengths_preserve_complete_json(padding: str) -> None:
    reply = Reply(
        body=REGISTERED,
        headers=(("Content-Length", padding + str(len(REGISTERED))),),
        declared_length=False,
    )
    with FakeFocus((reply,)) as peer:
        response = permitted_send(
            ProviderTransport("focus", "fake", peer.origin),
            "FC-NFE",
            ProviderRequest(),
        )
        assert response.body == REGISTERED
        assert _registration_result(response).state == CallState.SUCCESS


@pytest.mark.parametrize("body", [REGISTERED, REFUSED], ids=["registered", "refused"])
@pytest.mark.parametrize(
    "diagnostic",
    [
        b'"\xed\xa0\x80"',
        b'"\xed\xbf\xbf"',
        b'"\xc0\x80"',
        b'"\x80"',
        b'"\xe2\x82"',
        b'"\xf4\x90\x80\x80"',
        b'"\\ud800"',
        b'"\\udc00"',
        b'{"nested":["\\ud800"]}',
        b'{"\\udc00":"ignored"}',
    ],
    ids=[
        "reviewer-encoded-surrogate",
        "encoded-low-surrogate",
        "overlong",
        "orphan-continuation",
        "truncated-multibyte",
        "out-of-range",
        "escaped-high-surrogate",
        "escaped-low-surrogate",
        "nested-surrogate",
        "surrogate-object-key",
    ],
)
def test_invalid_unicode_anywhere_prevents_either_terminal_outcome(
    body: bytes, diagnostic: bytes
) -> None:
    response = ProviderResponse(200, body[:-1] + b',"mensagem":' + diagnostic + b"}")
    invalid = None
    try:
        response.json()
    except TransportFailure as error:
        invalid = error
    assert invalid is not None
    assert invalid.code == "resposta_invalida"
    result = _registration_result(response)
    assert (result.state, result.provider_code) == (
        CallState.UNCERTAIN,
        "resposta_invalida",
    )


@pytest.mark.parametrize("body", [REGISTERED, REFUSED], ids=["registered", "refused"])
@pytest.mark.parametrize(
    "encoding",
    ["utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"],
)
def test_json_is_not_autodetected_as_another_unicode_encoding(
    body: bytes, encoding: str
) -> None:
    response = ProviderResponse(200, body.decode("ascii").encode(encoding))
    invalid = None
    try:
        response.json()
    except TransportFailure as error:
        invalid = error
    assert invalid is not None
    assert invalid.code == "resposta_invalida"
    result = _registration_result(response)
    assert (result.state, result.provider_code) == (
        CallState.UNCERTAIN,
        "resposta_invalida",
    )


@pytest.mark.parametrize(
    ("body", "state"),
    [(REGISTERED, CallState.SUCCESS), (REFUSED, CallState.FAILED)],
    ids=["registered", "refused"],
)
@pytest.mark.parametrize(
    "diagnostic",
    [
        b'"caf\xc3\xa9"',
        b'"\xf0\x9f\x98\x80"',
        b'"\\ud83d\\ude00"',
        b'{"nested":["caf\xc3\xa9"],"\\ud83d\\ude00":"valid"}',
    ],
    ids=["multibyte", "non-bmp", "escaped-surrogate-pair", "nested-and-key"],
)
def test_valid_unicode_preserves_both_terminal_controls(
    body: bytes, state: str, diagnostic: bytes
) -> None:
    response = ProviderResponse(200, body[:-1] + b',"mensagem":' + diagnostic + b"}")
    assert isinstance(response.json(), dict)
    assert _registration_result(response).state == state
