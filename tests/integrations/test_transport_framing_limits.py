"""HTTPS uses the same complete framing and operation-specific bounds as HTTP."""

from pathlib import Path

import pytest

from apps.integrations.call_models import CallState
from apps.integrations.focus.ciencia_wire import _registration_result
from apps.integrations.transport_headers import FRAMING_LIMIT
from apps.integrations.transport_types import ProviderResponse, TransportFailure
from apps.integrations.transport_wire import XML_LIMIT
from tests.fakes.focus import FakeFocus
from tests.fakes.provider import Reply
from tests.integrations.test_transport_representation import (
    REFUSED,
    REGISTERED,
    wire_outcome,
)
from tests.integrations.transport_tls import tls_context


@pytest.mark.parametrize("body", [REGISTERED, REFUSED], ids=["registered", "refused"])
@pytest.mark.parametrize("complete", [False, True], ids=["incomplete", "complete"])
def test_verified_tls_requires_the_final_chunk_terminator(
    tmp_path: Path, body: bytes, complete: bool
) -> None:
    context, ca = tls_context(tmp_path)
    reply = Reply(
        body=f"{len(body):x}\r\n".encode()
        + body
        + b"\r\n0\r\n"
        + (b"\r\n" if complete else b""),
        headers=(("Transfer-Encoding", "chunked"),),
        declared_length=False,
    )
    with FakeFocus((reply,), tls=context) as peer:
        outcome = wire_outcome(peer, ca)
        if complete:
            assert isinstance(outcome, ProviderResponse), outcome
            expected = CallState.SUCCESS if body == REGISTERED else CallState.FAILED
            assert _registration_result(outcome).state == expected
        else:
            assert isinstance(outcome, TransportFailure), outcome
            assert _registration_result(outcome).state == CallState.UNCERTAIN
        assert len(peer.requests) == 1


@pytest.mark.parametrize(
    "chunk_size",
    [format(XML_LIMIT + 1, "x").encode(), b"f" * 4301],
    ids=["body-limit-plus-one", "unbounded-hex"],
)
def test_declared_chunk_oversize_is_rejected_before_reading_body(
    chunk_size: bytes,
) -> None:
    # No chunk data follows: the declared-size decision must precede a read.
    reply = Reply(
        body=chunk_size + b"\r\n",
        headers=(("Transfer-Encoding", "chunked"),),
        declared_length=False,
    )
    with FakeFocus((reply,)) as peer:
        outcome = wire_outcome(peer)
        assert isinstance(outcome, TransportFailure), outcome
        assert outcome.code == "resposta_excede_limite"
        assert len(peer.requests) == 1


def test_many_short_trailers_share_one_bounded_section() -> None:
    reply = Reply(
        body=(
            f"{len(REGISTERED):x}\r\n".encode()
            + REGISTERED
            + b"\r\n0\r\n"
            + b"X-Note: ignored\r\n" * 5000
            + b"\r\n"
        ),
        headers=(("Transfer-Encoding", "chunked"),),
        declared_length=False,
    )
    with FakeFocus((reply,)) as peer:
        outcome = wire_outcome(peer)
        assert isinstance(outcome, TransportFailure), outcome
        assert outcome.code == "resposta_excede_limite"


@pytest.mark.parametrize("coding", ["chunked ", "chunked\t", " Chunked "])
def test_valid_chunk_framing_survives_transfer_coding_whitespace(coding: str) -> None:
    reply = Reply(
        body=f"{len(REGISTERED):x}\r\n".encode() + REGISTERED + b"\r\n0\r\n\r\n",
        headers=(("Transfer-Encoding", coding),),
        declared_length=False,
    )
    with FakeFocus((reply,)) as peer:
        outcome = wire_outcome(peer)
        assert isinstance(outcome, ProviderResponse), outcome
        assert outcome.body == REGISTERED
        assert _registration_result(outcome).state == CallState.SUCCESS


def test_multiple_chunks_preserve_the_entire_body() -> None:
    cut = len(REGISTERED) // 2
    first, second = REGISTERED[:cut], REGISTERED[cut:]
    reply = Reply(
        body=(
            f"{len(first):x}\r\n".encode()
            + first
            + b"\r\n"
            + f"{len(second):x}\r\n".encode()
            + second
            + b"\r\n0\r\n\r\n"
        ),
        headers=(("Transfer-Encoding", "chunked"),),
        declared_length=False,
    )
    with FakeFocus((reply,)) as peer:
        outcome = wire_outcome(peer)
        assert isinstance(outcome, ProviderResponse), outcome
        assert outcome.body == REGISTERED
        assert _registration_result(outcome).state == CallState.SUCCESS


def chunked_reply(size_line: bytes) -> Reply:
    return Reply(
        body=size_line + REGISTERED + b"\r\n0\r\n\r\n",
        headers=(("Transfer-Encoding", "chunked"),),
        declared_length=False,
    )


def test_chunk_size_line_one_byte_over_the_framing_limit_is_refused() -> None:
    # Added over 8f63bbf: a CRLF-terminated size line readline can still return whole.
    size = f"{len(REGISTERED):x}".encode()
    line = b"0" * (FRAMING_LIMIT + 1 - len(size) - 2) + size + b"\r\n"
    assert len(line) == FRAMING_LIMIT + 1
    with FakeFocus((chunked_reply(line),)) as peer:
        outcome = wire_outcome(peer)
        assert isinstance(outcome, TransportFailure), outcome
        assert outcome.code == "resposta_invalida"
        assert len(peer.requests) == 1


def test_zero_padded_chunk_size_beyond_the_cap_digits_is_still_read() -> None:
    # Added over 8f63bbf: leading zeroes never count toward the stream cap.
    size = b"0" * 32 + f"{len(REGISTERED):x}".encode()
    assert len(size) > len(format(XML_LIMIT, "x"))
    with FakeFocus((chunked_reply(size + b"\r\n"),)) as peer:
        outcome = wire_outcome(peer)
        assert isinstance(outcome, ProviderResponse), outcome
        assert outcome.body == REGISTERED
        assert _registration_result(outcome).state == CallState.SUCCESS
