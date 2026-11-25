"""A terminal event result requires a complete, readable HTTP representation."""

import pytest

from apps.integrations.call_models import CallState
from apps.integrations.focus.ciencia_wire import _registration_result
from apps.integrations.transport_types import ProviderResponse, TransportFailure

REGISTERED = (
    b'{"status":"evento_registrado","status_sefaz":"135",'
    b'"tipo":"ciencia","protocolo":"891170005150285"}'
)
REFUSED = b'{"status":"erro","status_sefaz":"596","tipo":"ciencia"}'


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
