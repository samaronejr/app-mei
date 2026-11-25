"""Closed post-send outcomes, with separate native admission/persistence witnesses."""

from __future__ import annotations

import json
from collections import Counter
from itertools import product
from typing import TYPE_CHECKING

import pytest

from apps.integrations.call_models import CallState
from apps.integrations.focus.ciencia_wire import _registration_result
from apps.integrations.transport_types import ProviderResponse, TransportFailure

if TYPE_CHECKING:
    from collections.abc import Callable

MISSING = object()

# Tags describe the fixtures, independently of the implementation's predicates.
OUTCOMES: tuple[tuple[str, object], ...] = (
    ("registered", "evento_registrado"),
    ("refused", "erro"),
    ("unknown", "unknown"),
    ("missing", MISSING),
    ("null", None),
    ("integer", 135),
    ("array", []),
)
SEFAZ: tuple[tuple[str, object], ...] = (
    ("accepted", "135"),
    ("accepted", "136"),
    ("refused", "496"),
    ("refused", "594"),
    ("refused", "596"),
    ("refused", "650"),
    ("refused", "655"),
    ("refused", "658"),
    ("duplicate", "573"),
    ("batch", "128"),
    ("unknown", "999"),
    ("missing", MISSING),
    ("null", None),
    ("integer", 135),
    ("array", ["135"]),
    ("whitespace", "135 "),
)
PROTOCOLS: tuple[tuple[str, object], ...] = (
    ("valid", "891170005150285"),
    ("valid", "89117000515028512"),
    ("valid", "111111111111111"),
    ("valid", "10000000000000000"),
    ("sixteen", "8911700051502851"),
    ("short", "89117000515028"),
    ("long", "891170005150285123"),
    ("zero", "000000000000000"),
    ("zero", "00000000000000000"),
    ("unicode", "\u0661" * 15),
    ("whitespace", "891170005150285 "),
    ("blank", ""),
    ("integer", 891170005150285),
    ("boolean", True),
    ("array", []),
    ("object", {}),
    ("absent", None),
    ("absent", MISSING),
)
TYPES: tuple[tuple[str, object], ...] = (
    ("ciencia", "ciencia"),
    ("other", "confirmacao"),
    ("missing", MISSING),
    ("null", None),
    ("integer", 1),
    ("array", []),
    ("object", {}),
    ("whitespace", "ciencia "),
)
READABILITY: tuple[
    tuple[str, Callable[[bytes], ProviderResponse | TransportFailure]], ...
] = (
    ("json", lambda body: ProviderResponse(200, body)),
    ("empty", lambda _body: ProviderResponse(200, b"")),
    (
        "gzip-empty",
        lambda _body: TransportFailure("codificacao_nao_permitida", 200),
    ),
    (
        "gzip-body",
        lambda _body: TransportFailure("codificacao_nao_permitida", 200),
    ),
    (
        "declared-size-cap",
        lambda _body: TransportFailure("resposta_excede_limite", 200),
    ),
    (
        "streamed-size-cap",
        lambda _body: TransportFailure("resposta_excede_limite", 200),
    ),
    ("truncated-framing", lambda _body: TransportFailure("conexao_interrompida")),
    ("mixed-framing", lambda _body: TransportFailure("resposta_invalida", 200)),
    (
        "unsupported-transfer-coding",
        lambda _body: TransportFailure("codificacao_nao_permitida", 200),
    ),
    ("reset", lambda _body: TransportFailure("conexao_interrompida")),
    ("timeout", lambda _body: TransportFailure("tempo_limite")),
    ("truncated-json", lambda body: ProviderResponse(200, body[:-1])),
    (
        "non-json",
        lambda _body: ProviderResponse(200, b"<html>unreadable provider reply</html>"),
    ),
    ("non-object", lambda body: ProviderResponse(200, b"[" + body + b"]")),
    (
        "duplicate-members",
        lambda body: ProviderResponse(
            200,
            body[:-1] + (b"," if body != b"{}" else b"") + b'"probe":0,"probe":1}',
        ),
    ),
    ("trailing-junk", lambda body: ProviderResponse(200, body + b"trailing")),
    ("invalid-utf8", lambda body: ProviderResponse(200, body + b"\xff")),
    (
        "excessive-nesting",
        lambda _body: ProviderResponse(200, b"[" * 2000 + b"]" * 2000),
    ),
)


@pytest.mark.parametrize(
    ("readability", "receive"), READABILITY, ids=[row[0] for row in READABILITY]
)
def test_post_reply_cross_product_defaults_to_uncertain(
    readability: str, receive: Callable[[bytes], ProviderResponse | TransportFailure]
) -> None:
    """Only two tagged fixture combinations can ever settle the write."""
    expected_shapes = {
        ("json", "registered", "accepted", "valid", "ciencia"): CallState.SUCCESS,
        ("json", "refused", "refused", "absent", "ciencia"): CallState.FAILED,
    }
    counts: Counter[str] = Counter()
    for outcome, sefaz, protocol, kind in product(OUTCOMES, SEFAZ, PROTOCOLS, TYPES):
        tags = (readability, outcome[0], sefaz[0], protocol[0], kind[0])
        fields = {
            name: value
            for name, value in (
                ("status", outcome[1]),
                ("status_sefaz", sefaz[1]),
                ("protocolo", protocol[1]),
                ("tipo", kind[1]),
            )
            if value is not MISSING
        }
        reply = receive(json.dumps(fields).encode())
        expected = expected_shapes.get(tags, CallState.UNCERTAIN)
        result = _registration_result(reply)
        assert result.state == expected, (tags, fields, result)
        counts[result.state] += 1
    assert sum(counts.values()) == 16128
    assert counts[CallState.SUCCESS] == (8 if readability == "json" else 0)
    assert counts[CallState.FAILED] == (12 if readability == "json" else 0)


@pytest.mark.parametrize(
    "http_status", [201, 202, 204, 301, 302, 400, 401, 403, 404, 408, 429, 500, 503]
)
def test_non_200_success_shape_never_settles_post(http_status: int) -> None:
    for sefaz, protocol in product(("135", "136"), PROTOCOLS[:4]):
        body = json.dumps(
            {
                "status": "evento_registrado",
                "status_sefaz": sefaz,
                "tipo": "ciencia",
                "protocolo": protocol[1],
            }
        ).encode()
        result = _registration_result(ProviderResponse(http_status, body))
        assert result.state == CallState.UNCERTAIN
        assert result.http_status == http_status
