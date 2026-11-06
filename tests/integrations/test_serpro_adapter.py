"""SERPRO envelopes, escaped results, billing and definitive authentication refusals."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import (
    OperacaoNaoPermitida,
    ProviderResponse,
    TransportFailure,
)
from tests.fakes.provider import Reply, permitted_send
from tests.fakes.serpro import FakeSerpro
from tests.integrations.serpro_support import (
    CLIENT,
    CONTRATANTE,
    FIRM,
    serpro_module,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers" / "serpro"


def test_envelope_matches_golden_and_keeps_alphanumeric_cnpj_as_string() -> None:
    # Given three distinct parties and an alphanumeric client CNPJ.
    adapter = serpro_module("adapter")
    parties = adapter.Parties(CONTRATANTE, FIRM, CLIENT.lower())
    # When the request crosses the envelope boundary.
    request = adapter.build_request("SP-PAG", parties, {"tamanhoDaPagina": 100})
    # Then the wire shape, including escaped JSON and uppercase strings, is exact.
    assert json.loads(request.body) == json.loads(
        (FIXTURES / "envelope.json").read_text()
    )


@pytest.mark.parametrize("party", [12345678000195, True, None, "", "bad\ncnpj"])
def test_malformed_party_is_refused_before_network(party: str | int | None) -> None:
    # Given input which cannot represent a CNPJ string.
    adapter = serpro_module("adapter")
    # When it crosses the party boundary.
    with pytest.raises(adapter.CnpjInvalidoError):
        adapter.Parties(party, FIRM, CLIENT)
    # Then no transport or persisted request can exist.


@pytest.mark.parametrize(
    ("operation", "system", "service", "path"),
    [
        ("SP-TERMO", "AUTENTICAPROCURADOR", "ENVIOXMLASSINADO81", "/Apoiar"),
        ("SP-SIT-P", "SITFIS", "SOLICITARPROTOCOLO91", "/Apoiar"),
        ("SP-SIT-R", "SITFIS", "RELATORIOSITFIS92", "/Emitir"),
        ("SP-PAG", "PAGTOWEB", "PAGAMENTOS71", "/Consultar"),
        ("SP-COMP", "PAGTOWEB", "COMPARRECADACAO72", "/Emitir"),
        ("SP-DAS", "PGMEI", "GERARDASPDF21", "/Emitir"),
    ],
)
def test_catalog_envelope_uses_only_the_allowlisted_route(
    operation: str, system: str, service: str, path: str
) -> None:
    # Given an A6 operation and a real loopback peer.
    adapter = serpro_module("adapter")
    request = adapter.build_request(
        operation, adapter.Parties(CONTRATANTE, FIRM, CLIENT), {}
    )
    with FakeSerpro((Reply(),)) as peer:
        # When the envelope uses AXRM-011's allowlist and single-use window.
        permitted_send(
            ProviderTransport("serpro", "fake", peer.origin), operation, request
        )
        # Then only the registered operation reaches the expected path.
        assert [(r.method, r.path) for r in peer.requests] == [("POST", path)]
        pedido = json.loads(peer.requests[0].body)["pedidoDados"]
        assert (pedido["idSistema"], pedido["idServico"]) == (system, service)


@pytest.mark.parametrize(
    "operation",
    [
        "SP-AUTH",
        "Declarar",
        "TRANSDECLARACAO",
        "ATUBENEFICIO23",
        "EFETUAROPCAOREGIME101",
        "SOLICRENUNCIA262",
        "unknown",
    ],
)
def test_excluded_operation_never_builds_a_request(operation: str) -> None:
    # Given an operation outside the closed A6 service register.
    adapter = serpro_module("adapter")
    # When a caller tries to construct it.
    with pytest.raises(OperacaoNaoPermitida):
        adapter.build_request(operation, adapter.Parties(CONTRATANTE, FIRM, CLIENT), {})
    # Then neither an arbitrary path nor a declaration can reach transport.


@pytest.mark.parametrize(
    ("status", "billed"),
    [
        (200, "sim"),
        (202, "sim"),
        (204, "nao"),
        (304, "nao"),
        (400, "nao"),
        (401, "nao"),
        (403, "sim"),
        (404, "nao"),
        (429, "nao"),
        (500, "nao"),
        (503, "nao"),
        (504, "nao"),
    ],
)
@pytest.mark.parametrize("operation", ["SP-PAG", "SP-SIT-R", "SP-COMP", "SP-DAS"])
def test_billing_status_table_is_independent_of_fiscal_success(
    operation: str, status: int, billed: str
) -> None:
    # Given a complete response from a paid endpoint.
    adapter = serpro_module("adapter")
    # When the status is classified.
    result = adapter.classify(operation, ProviderResponse(status, b""))
    # Then the A6 status table, not success/failure, determines billing.
    assert (result.http_status, result.billed) == (status, billed)


@pytest.mark.parametrize("operation", ["SP-TERMO", "SP-SIT-P"])
@pytest.mark.parametrize("status", [200, 202, 204, 304, 400, 401, 403, 500, 503, 504])
def test_free_support_operations_never_claim_a_billed_unit(
    operation: str, status: int
) -> None:
    # Given a complete response to a free /Apoiar operation.
    adapter = serpro_module("adapter")
    # When it is classified, then even a billed error status has no unit price.
    assert adapter.classify(operation, ProviderResponse(status, b"")).billed == "nao"


def test_malformed_403_body_remains_a_billed_denial() -> None:
    # Given a complete 403 whose body is not the documented JSON envelope.
    adapter = serpro_module("adapter")
    # When it is classified, then the status alone decides denial and billing.
    result = adapter.classify("SP-PAG", ProviderResponse(403, b"<html>denied</html>"))
    assert (result.state, result.billed, result.provider_code) == (
        "negada",
        "sim",
        "serpro_http_403",
    )


def test_response_parser_decodes_escaped_data_without_losing_messages() -> None:
    # Given the provider's two-level JSON encoding.
    adapter = serpro_module("adapter")
    raw = json.dumps(
        {
            "status": 200,
            "mensagens": [{"codigo": "SUCESSO", "texto": "synthetic provider text"}],
            "dados": json.dumps({"pagamentos": [{"numeroDocumento": "0001"}]}),
        }
    ).encode()
    # When the response boundary parses it.
    parsed = adapter.parse_response(ProviderResponse(200, raw))
    # Then consumers receive typed metadata and decoded fields, not escaped JSON.
    assert parsed.status == 200
    assert parsed.codes == ("SUCESSO",)
    assert parsed.data == {"pagamentos": [{"numeroDocumento": "0001"}]}


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        b"[]",
        b'{"status":200,"dados":"not-json"}',
        b'{"status":200,"status":403,"dados":"{}"}',
    ],
)
def test_malformed_provider_data_is_a_symbolic_failure(raw: bytes) -> None:
    # Given malformed or ambiguous provider JSON.
    adapter = serpro_module("adapter")
    # When it crosses the response boundary.
    with pytest.raises(TransportFailure):
        adapter.parse_response(ProviderResponse(200, raw))
    # Then raw provider text never becomes a persisted error.


def test_complete_401_is_distinguishable_from_partial_401_failure() -> None:
    # Given the same HTTP status, once complete and once from a broken body.
    adapter = serpro_module("adapter")
    complete = adapter.classify("SP-DAS", ProviderResponse(401, b""))
    partial = replace(
        TransportFailure("conexao_interrompida", 401).result(),
        billed="nao",
        settled_microbrl=0,
    )
    # When the protocol asks for definitive authentication refusal.
    retry = serpro_module("reauthentication")
    # Then only a complete 401 can authorize the narrow retry path.
    assert retry.definitive_401(complete)
    assert not retry.definitive_401(partial)


def test_304_termo_token_survives_wire_filtering_but_not_diagnostic_repr() -> None:
    # Given the documented body-less response carrying token and expiry in headers.
    adapter = serpro_module("adapter")
    token = str(UUID(int=13))
    request = adapter.build_request(
        "SP-TERMO",
        adapter.Parties(CONTRATANTE, FIRM, FIRM),
        {"xml": "PHRlcm1vLz4="},
    )
    with FakeSerpro(
        (
            Reply(
                status=304,
                body=b"",
                headers=(
                    ("etag", f"autenticar_procurador_token:{token}"),
                    ("expires", "Mon, 25 Oct 2027 00:00:00 GMT"),
                ),
            ),
        )
    ) as peer:
        # When the ordinary single-use transport supplies the actual response.
        response = permitted_send(
            ProviderTransport("serpro", "fake", peer.origin), "SP-TERMO", request
        )
        parsed = adapter.parse_termo_response(response)
    # Then header filtering cannot discard authority, or leak it through repr.
    assert parsed.procurador_token == token
    assert parsed.expires_at == datetime(2027, 10, 25, tzinfo=UTC).timestamp()
    assert token not in repr(parsed) + repr(response)


def test_200_termo_reads_the_documented_expiration_field() -> None:
    # Given the current provider's escaped Autentica payload, not an invented TTL.
    adapter = serpro_module("adapter")
    expected = "synthetic-termo-token"
    body = json.dumps(
        {
            "status": 200,
            "mensagens": [],
            "dados": json.dumps(
                {
                    "autenticar_procurador_token": expected,
                    "data_hora_expiracao": "2027-10-25T00:00:00",
                }
            ),
        }
    ).encode()
    # When the response is parsed, then it retains only transient token material.
    parsed = adapter.parse_termo_response(ProviderResponse(200, body))
    assert parsed.procurador_token == expected
    assert parsed.expires_at == datetime(2027, 10, 25, tzinfo=UTC).timestamp()
    assert expected not in repr(parsed)
