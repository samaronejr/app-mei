"""Raw length fields must be validated before a library converts their values."""

import http.client

import pytest
import urllib3.response

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


def observe_library_conversions(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str]]:
    """Delegate every real conversion, observing only response initialization."""
    seen: list[tuple[str, str]] = []
    begin = http.client.HTTPResponse.begin
    initialize = urllib3.response.HTTPResponse._init_length

    def observed_begin(response: http.client.HTTPResponse) -> None:
        def convert(value: str | bytes | int) -> int:
            seen.append(("http.client", str(value)))
            return int(value)

        with monkeypatch.context() as scope:
            scope.setattr(http.client, "int", convert, raising=False)
            begin(response)

    def observed_initialize(
        response: urllib3.response.HTTPResponse, request_method: str | None
    ) -> int | None:
        def convert(value: str | bytes | int) -> int:
            seen.append(("urllib3.response", str(value)))
            return int(value)

        with monkeypatch.context() as scope:
            scope.setattr(urllib3.response, "int", convert, raising=False)
            return initialize(response, request_method)

    monkeypatch.setattr(http.client.HTTPResponse, "begin", observed_begin)
    monkeypatch.setattr(
        urllib3.response.HTTPResponse, "_init_length", observed_initialize
    )
    return seen


@pytest.mark.parametrize("body", [REFUSED, REGISTERED], ids=["refused", "registered"])
@pytest.mark.parametrize(
    "fields",
    [
        (("Content-Length ", "{short}"),),
        (("Content-Length\t", "{short}"),),
        (("Content Length", "{short}"),),
        (("X-Unparsed ", "ignored"),),
        (("X-Control", "\x00"),),
        (("Content-Length", "{length}"), ("Content-Length ", "{short}")),
        (("Content-Length", ""),),
        (("Content-Length", " \t "),),
        (("Content-Length", "-1"),),
        (("Content-Length", "+{length}"),),
        (("Content-Length", "{length}.0"),),
        (("Content-Length", "5 5"),),
        (("Content-Length", "{length},{short}"),),
        (("Content-Length", "{length}"), ("content-length", "{short}")),
        # An empty edge is legal (see the terminal controls below); it must
        # not hide a malformed or disagreeing nonempty member.
        (("Content-Length", "{length},+{length},"),),
        (("Content-Length", ",{length}, ,{short}"),),
        (("Content-Length", " , \t,"),),
        (("Content-Length", "," * 17 + "{length}"),),
        (("Content-Length", "5\r\n 5"),),
        (("Content-Length", "{length}\r\n Content-Length: {length}"),),
        (("Content-Length", "0" * 21 + "{length}"),),
    ],
    ids=[
        "reviewer-space-before-colon",
        "tab-before-colon",
        "space-inside-name",
        "unparsed-other-field",
        "control-in-other-field",
        "malformed-second-length",
        "empty",
        "only-ows",
        "negative",
        "signed",
        "fractional",
        "internal-space",
        "unequal-comma-list",
        "unequal-duplicate-fields",
        "empty-last-list-member",
        "empty-first-list-member",
        "no-numeric-list-member",
        "too-many-empty-members",
        "obs-fold-between-digits",
        "obs-fold-hidden-field",
        "digit-count-bound",
    ],
)
def test_malformed_length_fields_never_settle_either_body(
    body: bytes, fields: tuple[tuple[str, str], ...]
) -> None:
    headers = tuple(
        (name, value.format(length=len(body), short=len(body) + 1))
        for name, value in fields
    )
    with FakeFocus((Reply(body=body, headers=headers, declared_length=False),)) as peer:
        outcome = wire_outcome(peer)
        assert isinstance(outcome, TransportFailure), outcome
        assert _registration_result(outcome).state == CallState.UNCERTAIN
        assert outcome.code
        assert len(peer.requests) == 1


@pytest.mark.parametrize(
    ("body", "expected"),
    [(REFUSED, CallState.FAILED), (REGISTERED, CallState.SUCCESS)],
    ids=["refused", "registered"],
)
@pytest.mark.parametrize(
    "fields",
    [
        (("Content-Length", "{length} "),),
        (("Content-Length", "{length}\t"),),
        (("Content-Length", "\t {length} \t"),),
        (("Content-Length", "000{length}"),),
        (("Content-Length", "0" * 18 + "{length}"),),
        (("Content-Length", "{length}, {length}"),),
        (("Content-Length", "0{length},\t{length} "),),
        (("Content-Length", "{length}"), ("content-length", "0{length} ")),
        (("Content-Length", "\r\n\t{length}\r\n\t"),),
        (("Content-Length", "{length},\r\n\t{length}"),),
        (("cOnTeNt-LeNgTh", "{length}"),),
        (("Content-Length", "{length},"),),
        (("Content-Length", ",{length}"),),
        (("Content-Length", "{length}, ,{length}"),),
        (("Content-Length", "," * 16 + "{length}"),),
        (("Content-Length", ""), ("content-length", " ,0{length}, \t")),
    ],
    ids=[
        "reviewer-trailing-space",
        "trailing-tab",
        "surrounding-ows",
        "leading-zeroes",
        "digit-count-at-bound",
        "equal-comma-list",
        "equal-numeric-list",
        "equal-duplicate-fields",
        "obs-fold-outer-ows",
        "obs-fold-equal-list",
        "case-insensitive-name",
        "empty-last-list-member",
        "empty-first-list-member",
        "reviewer-empty-middle-member",
        "empty-members-at-bound",
        "empty-and-numeric-field-lines",
    ],
)
def test_valid_length_fields_preserve_both_terminal_controls(
    body: bytes,
    expected: str,
    fields: tuple[tuple[str, str], ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = observe_library_conversions(monkeypatch)
    headers = tuple((name, value.format(length=len(body))) for name, value in fields)
    with FakeFocus((Reply(body=body, headers=headers, declared_length=False),)) as peer:
        outcome = wire_outcome(peer)
        assert isinstance(outcome, ProviderResponse), outcome
        assert outcome.body == body
        assert _registration_result(outcome).state == expected
        length_calls = [(owner, value) for owner, value in seen if value != "200"]
        assert [owner for owner, _value in length_calls] == [
            "http.client",
            "urllib3.response",
        ]
        for _owner, value in length_calls:
            assert value == str(len(body))
        assert len(peer.requests) == 1


@pytest.mark.parametrize(
    "declared",
    [str(XML_LIMIT + 1), "9" * 4301, "0" * 4301, "-1", "55,56"],
    ids=[
        "reviewer-one-over-limit",
        "huge-value",
        "huge-zeroes",
        "negative",
        "conflict",
    ],
)
def test_length_bounds_precede_every_library_integer_conversion(
    declared: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = observe_library_conversions(monkeypatch)
    wire = f"HTTP/1.1 200 OK\r\nContent-Length: {declared}\r\n\r\n".encode()
    with FakeFocus((Reply(raw_response=wire),)) as peer:
        outcome = wire_outcome(peer)
        assert isinstance(outcome, TransportFailure), outcome
        assert _registration_result(outcome).state == CallState.UNCERTAIN
        assert not [(owner, value) for owner, value in seen if value != "200"]
        assert len(peer.requests) == 1


@pytest.mark.parametrize(
    ("section", "valid"),
    [
        (b"Content-Length: 00000\r\n\r\n", True),
        (b"\t55\r\nContent-Length: 0\r\n\r\n", False),
        (b"Content-Length: 0\r\n", False),
        (b"X-Note: text\nContent-Length: 0\r\n\r\n", False),
        (b"X-Note: \x7f\r\nContent-Length: 0\r\n\r\n", False),
        (b"X-Note: text\r\n\tbad\x01\r\nContent-Length: 0\r\n\r\n", False),
        (b"X: " + b"a" * (65536 - 7) + b"\r\n\r\n", True),
        (b"X: " + b"a" * (65536 - 6) + b"\r\n\r\n", False),
        (b"X: a\r\n" * 99 + b"\r\n", True),
        (b"X: a\r\n" * 100 + b"\r\n", False),
    ],
    ids=[
        "zero-with-leading-zeroes",
        "orphan-continuation",
        "missing-section-terminator",
        "bare-lf",
        "control-in-field",
        "control-in-continuation",
        "section-at-byte-limit",
        "section-one-byte-over",
        "section-at-line-limit",
        "section-one-line-over",
    ],
)
def test_raw_field_section_is_complete_and_bounded(section: bytes, valid: bool) -> None:
    with FakeFocus((Reply(raw_response=b"HTTP/1.1 200 OK\r\n" + section),)) as peer:
        outcome = wire_outcome(peer)
        if valid:
            assert isinstance(outcome, ProviderResponse), outcome
            assert outcome.body == b""
        else:
            assert isinstance(outcome, TransportFailure), outcome
            assert _registration_result(outcome).state == CallState.UNCERTAIN
            assert outcome.code
        assert len(peer.requests) == 1


@pytest.mark.parametrize("valid", [True, False], ids=["continue", "malformed-continue"])
def test_informational_response_cannot_hide_a_field(valid: bool) -> None:
    interim = b"X-Note: continue" if valid else b"Content-Length : 0"
    wire = (
        b"HTTP/1.1 100 Continue\r\n" + interim + b"\r\n\r\n"
        b"HTTP/1.1 200 OK\r\nContent-Length: 55 \r\n\r\n" + REFUSED
    )
    with FakeFocus((Reply(raw_response=wire),)) as peer:
        outcome = wire_outcome(peer)
        if valid:
            assert isinstance(outcome, ProviderResponse), outcome
            assert _registration_result(outcome).state == CallState.FAILED
        else:
            assert isinstance(outcome, TransportFailure), outcome
            assert _registration_result(outcome).state == CallState.UNCERTAIN
        assert len(peer.requests) == 1


@pytest.mark.parametrize("over", [False, True], ids=["at-limit", "one-byte-over"])
def test_length_boundary_is_shared_by_declaration_and_body(over: bool) -> None:
    body = REFUSED + b" " * (XML_LIMIT + int(over) - len(REFUSED))
    with FakeFocus((Reply(body=body),)) as peer:
        outcome = wire_outcome(peer)
        if over:
            assert isinstance(outcome, TransportFailure), outcome
            assert outcome.code == "resposta_excede_limite"
        else:
            assert isinstance(outcome, ProviderResponse), outcome
            assert len(outcome.body) == XML_LIMIT
            assert _registration_result(outcome).state == CallState.FAILED
        assert len(peer.requests) == 1
