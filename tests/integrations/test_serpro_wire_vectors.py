"""Vendor examples are independent of the SERPRO runtime catalog."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date
from http.client import HTTPConnection
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from apps.core.tenancy import tenant_context
from apps.integrations import allowlist
from apps.integrations.call_contracts import AdmissionDeniedError, CallRequest
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_protocol import begin_io, claim, reserve
from apps.integrations.provider_journal import read as read_journal
from apps.integrations.serpro import sitfis_qualification
from apps.integrations.serpro.codec import Parties, build_request
from apps.integrations.serpro.sitfis_qualification import _fake_origin
from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import OperacaoNaoPermitida, ProviderRequest
from apps.obligations.models import FederalConsultation
from apps.tenants.models import Tenant
from tests.fakes.provider import Reply, permitted_send
from tests.fakes.serpro import FakeSerpro
from tests.integrations.serpro_support import CLIENT, CONTRATANTE, FIRM
from tests.integrations.serpro_wire_clock import wire_clock
from tests.integrations.sitfis_support import (
    World,
    clock,
    protocol_reply,
    report_reply,
    termo_reply,
    world,
)

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from apps.integrations.transport_types import Environment, JSONValue
    from tests.integrations.sitfis_support import Clock

__all__ = ["clock", "wire_clock", "world"]

WIRE = Path(__file__).parents[1] / "fixtures" / "providers" / "serpro" / "wire"
SERVICES = {
    "SP-TERMO": ("ENVIOXMLASSINADO81", "/Apoiar"),
    "SP-SIT-P": ("SOLICITARPROTOCOLO91", "/Apoiar"),
    "SP-SIT-R": ("RELATORIOSITFIS92", "/Emitir"),
    "SP-PAG": ("PAGAMENTOS71", "/Consultar"),
    "SP-COMP": ("COMPARRECADACAO72", "/Emitir"),
    "SP-DAS": ("GERARDASPDF21", "/Emitir"),
}
PARTIES = Parties(CONTRATANTE, FIRM, CLIENT.lower())
VERSION_ERROR = "EntradaIncorreta-ICGERENCIADOR-040"


def _fixture(operation: str) -> dict[str, Any]:
    return json.loads((WIRE / f"{SERVICES[operation][0]}.json").read_text())


def _expected(operation: str) -> dict[str, Any]:
    body = deepcopy(_fixture(operation)["body"])
    # This application only sends CNPJs, not the CPF identifiers in some examples.
    for field, cnpj in (
        ("contratante", PARTIES.contratante),
        ("autorPedidoDados", PARTIES.firm),
        ("contribuinte", PARTIES.client),
    ):
        body[field] = {"numero": cnpj, "tipo": 2}
    return body


def _post(
    origin: str, path: str, body: dict[str, Any] | bytes, *, method: str = "POST"
) -> tuple[int, bytes]:
    connection = HTTPConnection(origin.removeprefix("http://"), timeout=5)
    try:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        connection.request(method, path, raw)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


@pytest.mark.parametrize("peer_kind", ["scripted", "qualification"])
@pytest.mark.parametrize("encoding", ["utf-16le", "utf-16be", "utf-8-sig", "invalid"])
@pytest.mark.parametrize("nested", [False, True], ids=["envelope", "dados"])
def test_fake_rejects_non_utf8_json(
    peer_kind: str, encoding: str, nested: bool
) -> None:
    operation = "SP-SIT-R" if nested else "SP-SIT-P"
    vector = _fixture(operation)
    canonical = vector["body"]
    path = vector["route"][1]
    body = deepcopy(canonical)
    text = body["pedidoDados"]["dados"] if nested else json.dumps(body)
    if encoding == "invalid":
        raw = b"\xff" + text.encode()
        if nested:
            body["pedidoDados"]["dados"] = "\udcff" + text
    else:
        raw = text.encode(encoding)
        if nested:
            body["pedidoDados"]["dados"] = raw.decode("utf-8", errors="surrogateescape")
    bad = body if nested else raw
    if peer_kind == "scripted":
        with FakeSerpro((Reply(),)) as peer:
            status, refused = _post(peer.origin, path, bad)
            assert status == 400
            assert _post(peer.origin, path, canonical)[0] == 200
    else:
        with _fake_origin() as origin:
            status, refused = _post(origin, path, bad)
            assert status == 400
            assert _post(origin, path, canonical)[0] == 200
    assert json.loads(refused)["mensagens"] == [{"codigo": "fake_pedido_invalido"}]


@pytest.mark.parametrize("peer_kind", ["scripted", "qualification"])
@pytest.mark.parametrize(
    ("location", "value", "valid"),
    [
        ("dados", '{"protocoloRelatorio":"\udcff"}', False),
        ("dados", r'{"protocoloRelatorio":"\ud800"}', False),
        ("dados", r'{"protocoloRelatorio":"\udcff"}', False),
        ("dados", r'{"protocoloRelatorio":"\ud83d\ude00"}', True),
        ("dados", r'{"protocoloRelatorio":"synthetic","\ud800":0}', False),
        (
            "dados",
            r'{"protocoloRelatorio":"synthetic","extra":[{"value":"\udcff"}]}',
            False,
        ),
        ("envelope", {"extension": "\ud800"}, False),
        ("envelope", {"\udcff": "extension"}, False),
        ("envelope", {"extension": [{"value": "\udcff"}]}, False),
        ("envelope", {"extension": "\U0001f600"}, True),
    ],
    ids=[
        "dados-literal-low",
        "dados-escaped-high",
        "dados-escaped-low",
        "dados-pair",
        "dados-key",
        "dados-deep",
        "envelope-high",
        "envelope-key",
        "envelope-deep",
        "envelope-pair",
    ],
)
def test_fake_requires_unicode_scalar_json(
    peer_kind: str, location: str, value: JSONValue, valid: bool
) -> None:
    vector = _fixture("SP-SIT-R")
    canonical = vector["body"]
    body = deepcopy(canonical)
    if location == "dados":
        body["pedidoDados"]["dados"] = value
    else:
        assert isinstance(value, dict)
        body.update(value)
    path = vector["route"][1]
    if peer_kind == "scripted":
        with FakeSerpro((Reply(),)) as peer:
            status, response = _post(peer.origin, path, body)
            assert status == (200 if valid else 400)
            if not valid:
                assert _post(peer.origin, path, canonical)[0] == 200
    else:
        with _fake_origin() as origin:
            status, response = _post(origin, path, body)
            assert status == (200 if valid else 400)
    if not valid:
        assert json.loads(response)["mensagens"] == [{"codigo": "fake_pedido_invalido"}]


@pytest.mark.parametrize("operation", SERVICES)
def test_built_body_matches_official_example(operation: str) -> None:
    expected = _expected(operation)
    pedido = expected["pedidoDados"]
    example_data = pedido["dados"]
    data = json.loads(example_data) if example_data else ""
    if example_data:
        pedido["dados"] = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    actual = build_request(operation, PARTIES, data)
    assert (
        actual.body
        == json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode()
    )


@pytest.mark.parametrize("operation", ["SP-SIT-P", "SP-SIT-R"])
@pytest.mark.parametrize("environment", ["trial", "producao"])
def test_sitfis_selects_gateway_version(
    operation: str, environment: Environment
) -> None:
    vector = _fixture(operation)
    expected = _expected(operation)
    pedido = expected["pedidoDados"]
    if environment == "trial":
        pedido["versaoSistema"] = vector["trial_version"]
    data = json.loads(pedido["dados"]) if pedido["dados"] else ""
    request = build_request(operation, PARTIES, data, environment=environment)
    assert (
        json.loads(request.body)["pedidoDados"]["versaoSistema"]
        == pedido["versaoSistema"]
    )
    path = allowlist.resolve("serpro", operation, request, environment=environment)[1]
    assert path == SERVICES[operation][1]
    other = "trial" if environment == "producao" else "producao"
    with pytest.raises(OperacaoNaoPermitida):
        allowlist.resolve("serpro", operation, request, environment=other)


def test_only_pgmei_has_an_unverified_production_version() -> None:
    assert set(allowlist.SERPRO_SERVICES) == set(SERVICES)
    for operation in SERVICES:
        vector = _fixture(operation)
        contract = allowlist.SERPRO_SERVICES[operation]
        verified = operation != "SP-DAS"
        assert getattr(contract, "production_verified", None) is verified
        assert vector["production_verified"] is verified
        assert contract.production_page == vector["production_page"]
        source = "producao" if verified else "trial"
        assert contract.version_source_environment == source
        assert vector["version_source_environment"] == source
        assert (
            contract.production_version
            == vector["body"]["pedidoDados"]["versaoSistema"]
        )
        assert date.fromisoformat(contract.read_date) == date.fromisoformat(
            vector["read_date"]
        )
        if not verified:
            assert contract.trial_page == vector["trial_page"]
            assert contract.production_schema == vector["production_schema"]


@pytest.mark.parametrize("operation", ["SP-SIT-P", "SP-SIT-R"])
def test_legacy_sitfis_version_is_refused_before_any_request(operation: str) -> None:
    body = _expected(operation)
    body["pedidoDados"]["versaoSistema"] = "1.0"
    if operation == "SP-SIT-R":
        body["pedidoDados"]["dados"] = json.dumps(
            json.loads(body["pedidoDados"]["dados"]), separators=(",", ":")
        )
    request = ProviderRequest(body=json.dumps(body).encode())
    with FakeSerpro((Reply(),)) as peer:
        refusal = None
        try:
            permitted_send(
                ProviderTransport("serpro", "fake", peer.origin),
                operation,
                request,
            )
        except OperacaoNaoPermitida as error:
            refusal = type(error)
        assert not peer.requests
        # A missing SP-SIT-R wire permit is a different, later refusal.
        assert refusal is OperacaoNaoPermitida


@pytest.mark.parametrize(
    ("operation", "field", "value"),
    [
        ("SP-SIT-P", "versaoSistema", "1.0"),
        ("SP-SIT-R", "versaoSistema", "1.0"),
        ("SP-SIT-P", "dados", '""'),
        ("SP-SIT-R", "dados", ""),
        ("SP-SIT-R", "dados", {"protocoloRelatorio": "not a wire string"}),
        ("SP-SIT-R", "dados", "{broken"),
    ],
)
@pytest.mark.parametrize("peer_kind", ["scripted", "qualification"])
def test_fake_rejects_bad_wire_independently_of_transport(
    operation: str, field: str, value: JSONValue, peer_kind: str
) -> None:
    expected = _expected(operation)
    expected["pedidoDados"][field] = value
    path = SERVICES[operation][1]
    if peer_kind == "scripted":
        with FakeSerpro((Reply(),)) as peer:
            status, response = _post(peer.origin, path, expected)
    else:
        with _fake_origin() as origin:
            status, response = _post(origin, path, expected)
    assert status == 400
    if field == "versaoSistema":
        assert json.loads(response)["mensagens"][0]["codigo"] == VERSION_ERROR


@pytest.mark.parametrize("operation", SERVICES)
def test_fixture_backed_peer_accepts_the_built_envelope(operation: str) -> None:
    expected = _expected(operation)
    dados = expected["pedidoDados"]["dados"]
    request = build_request(operation, PARTIES, json.loads(dados) if dados else None)
    with FakeSerpro((Reply(),)) as peer:
        # Bypass the application allowlist so a sender regression also hits the peer.
        status, response = _post(
            peer.origin, SERVICES[operation][1], json.loads(request.body)
        )
    assert (status, response) == (200, Reply().body)


@pytest.mark.parametrize("peer_kind", ["scripted", "qualification"])
@pytest.mark.parametrize("operation", SERVICES)
def test_fake_requires_documented_input_shape(peer_kind: str, operation: str) -> None:
    vector = _fixture(operation)
    expected = vector["body"]
    _, path = vector["route"]
    variants: list[tuple[str, JSONValue]] = []
    dados = expected["pedidoDados"]["dados"]
    if dados:
        fields = json.loads(dados)
        variants.append(("empty", ""))
        variants.append(("array", "[]"))
        for key, value in fields.items():
            if key != "intervaloDataArrecadacao":
                variants.append(
                    (
                        f"missing-{key}",
                        json.dumps({k: v for k, v in fields.items() if k != key}),
                    )
                )
            variants.append(
                (f"type-{key}", json.dumps({**fields, key: not bool(value)}))
            )
        if operation == "SP-PAG":
            variants.append(
                (
                    "date-type",
                    json.dumps(
                        {
                            **fields,
                            "intervaloDataArrecadacao": {"dataInicial": 1},
                        }
                    ),
                )
            )
        if operation == "SP-DAS":
            variants.append(
                (
                    "consolidation-type",
                    json.dumps(
                        {
                            **fields,
                            "dataConsolidacao": 1,
                        }
                    ),
                )
            )
    else:
        variants.extend([("object", "{}"), ("encoded-empty", '""')])
    if dados:
        invalid = {
            "base64": "<xml/>",
            "text": " \t",
            "document-number": "é" * 9,
            "period": "\uff12\uff10\uff12\uff16\uff11\uff12",
            "page-offset": -1,
            "page-size": 101,
        }
        for key, rule in vector["dados"]["required"].items():
            variants.append(
                (f"format-{key}", json.dumps({**fields, key: invalid[rule]}))
            )
    else:
        variants.extend([("null", None), ("json-object", {})])
    if peer_kind == "scripted":
        with FakeSerpro((Reply(status=403), Reply(), Reply(), Reply())) as peer:
            wrong = deepcopy(expected)
            wrong["pedidoDados"]["versaoSistema"] = "9.0"
            assert _post(peer.origin, path, wrong)[0] == 400
            assert _post(peer.origin, path, expected)[0] == 403
            _assert_fake_variants(peer.origin, path, expected, variants)
            assert _post(peer.origin, path, expected)[0] == 200
    else:
        with _fake_origin() as origin:
            _assert_fake_variants(origin, path, expected, variants)


@pytest.mark.parametrize("peer_kind", ["scripted", "qualification"])
@pytest.mark.parametrize("attack", ["contractor-cpf", "raw-xml", "wrong-endpoint"])
def test_fake_rejects_wire_contract_attack(peer_kind: str, attack: str) -> None:
    operation = "SP-TERMO" if attack == "raw-xml" else "SP-SIT-P"
    body = _fixture(operation)["body"]
    path = SERVICES[operation][1]
    if attack == "contractor-cpf":
        body["contratante"] = {"numero": "99999999999", "tipo": 1}
    elif attack == "raw-xml":
        body["pedidoDados"]["dados"] = '{"xml":"<termo-sintetico-assinado/>"}'
    else:
        path = "/Emitir"
    if peer_kind == "scripted":
        with FakeSerpro((Reply(),)) as peer:
            response = _post(peer.origin, path, body)
    else:
        with _fake_origin() as origin:
            response = _post(origin, path, body)
    assert response[0] == 400


def _assert_fake_variants(
    origin: str,
    path: str,
    expected: dict[str, Any],
    variants: list[tuple[str, JSONValue]],
) -> None:
    assert _post(origin, path, expected)[0] == 200
    for label, value in variants:
        body = deepcopy(expected)
        body["pedidoDados"]["dados"] = value
        assert _post(origin, path, body)[0] == 400, label
    for party in ("contratante", "autorPedidoDados", "contribuinte"):
        body = deepcopy(expected)
        del body[party]
        assert _post(origin, path, body)[0] == 400, party
        body = deepcopy(expected)
        body[party]["tipo"] = True
        assert _post(origin, path, body)[0] == 400, f"type-{party}"
        for invalid in ("", "123.456.789-01", 99999999999999):
            body = deepcopy(expected)
            body[party]["numero"] = invalid
            assert _post(origin, path, body)[0] == 400, f"number-{party}"
    positive = deepcopy(expected)
    positive["contratante"] = {"numero": "12ABC34501DE35", "tipo": 2}
    positive["autorPedidoDados"] = {"numero": "00000000001", "tipo": 1}
    if positive["pedidoDados"]["idServico"] == "PAGAMENTOS71":
        positive["pedidoDados"]["dados"] = '{"primeiroDaPagina":0,"tamanhoDaPagina":1}'
    assert _post(origin, path, positive)[0] == 200
    body = deepcopy(expected)
    body["pedidoDados"]["idSistema"] = "OTHER"
    assert _post(origin, path, body)[0] == 400
    body = deepcopy(expected)
    del body["pedidoDados"]["versaoSistema"]
    assert _post(origin, path, body)[0] == 400
    if expected["pedidoDados"]["idServico"] == "GERARDASPDF21":
        body = deepcopy(expected)
        body["contribuinte"] = {"numero": "00000000001", "tipo": 1}
        assert _post(origin, path, body)[0] == 400
    assert _post(origin, path, expected, method="PUT")[0] in (400, 405, 501)


@pytest.mark.django_db(transaction=True)
def test_trial_sitfis_binds_the_trial_bytes_to_the_journal(
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wire_clock: Clock,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL = {"serpro": "10.00"}
    settings.USAGE_UNIT_PRICES_BRL = sitfis_qualification.SYNTHETIC_PRICES
    monkeypatch.setenv("SERPRO_TRIAL_BEARER", "synthetic-wire-test")

    # Keep production transport and protocol code; substitute only the network origin.
    with FakeSerpro(
        (termo_reply(), protocol_reply(0), report_reply()), trial=True
    ) as peer:
        monkeypatch.setattr(ProviderTransport, "_origin", lambda _self: peer.origin)
        result = sitfis_qualification.qualify_sitfis("trial")
    assert result.parsed_fields["state"] == "emitido"
    assert [request.path for request in peer.requests] == [
        "/Apoiar",
        "/Apoiar",
        "/Emitir",
    ]
    _, protocol, emission = peer.requests
    calls = result.parsed_fields["calls"]
    assert isinstance(calls, list)
    assert [call["operation"] for call in calls] == ["SP-SIT-P", "SP-SIT-R"]
    by_digest = {}
    for request, call in zip((protocol, emission), calls, strict=True):
        assert isinstance(call, dict)
        assert json.loads(request.body)["pedidoDados"]["versaoSistema"] == "1.0"
        assert call["request_digest"] == hashlib.sha256(request.body).hexdigest()
        by_digest[call["request_digest"]] = dict(request.headers)["X-Request-Tag"]
    assert json.loads(protocol.body)["pedidoDados"]["dados"] == ""
    # The synthetic trial created exactly one tenant and one consultation.
    tenant = Tenant.objects.get(slug__startswith="pq-serpro-")
    receipts = [record for record in read_journal(tenant.pk) if record.result is None]
    assert len(receipts) == 2
    with tenant_context(tenant.pk):
        rows = list(ProviderCall.objects.order_by("created_at"))
        consultation = FederalConsultation.objects.get()
        assert consultation.scope_digest == hashlib.sha256(protocol.body).hexdigest()
    before, after = rows
    assert before.created_at < after.created_at
    assert before.next_attempt_at == before.created_at
    assert after.next_attempt_at == after.created_at == wire_clock.now()
    for receipt in receipts:
        row = {row.pk: row for row in rows}[receipt.call_id]
        assert receipt.request_digest == row.request_digest
        assert by_digest[receipt.request_digest] == receipt.attempt_id.hex
        assert receipt.occurred_at == row.dispatch_started_at
        assert f"/{wire_clock.now():%Y/%m}/" in receipt.path


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("wire_clock")
@pytest.mark.parametrize("operation", ["SP-SIT-P", "SP-SIT-R"])
def test_old_revision_refused_at_dispatch(world: World, operation: str) -> None:
    old_body = _expected(operation)
    old_body["pedidoDados"]["versaoSistema"] = "1.0"
    if operation == "SP-SIT-P":
        old_body["pedidoDados"]["dados"] = '""'
    digest = hashlib.sha256(json.dumps(old_body, separators=(",", ":")).encode())
    key = hashlib.sha256(f"wire-revision-{operation}".encode()).hexdigest()
    call = reserve(
        world.tenant_id,
        world.link.pk,
        CallRequest(operation, key, digest.hexdigest(), key),
    )
    ticket = claim(world.tenant_id, call.pk)
    assert ticket is not None
    # Reproduce a claimed v1 row without depending on a sleep or on a deployment.
    with tenant_context(world.tenant_id):
        ProviderCall.objects.filter(pk=call.pk).update(operation_revision="v1")
    reason = None
    try:
        begin_io(ticket)
    except AdmissionDeniedError as error:
        reason = str(error)
    with tenant_context(world.tenant_id):
        call.refresh_from_db()
    assert (call.state, call.attempts, call.dispatch_started_at) == (
        CallState.AUTHORITY,
        0,
        None,
    )
    assert reason == CallState.AUTHORITY
    assert world.gateway.requests == []
