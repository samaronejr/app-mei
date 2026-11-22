"""Vendor examples are independent of the SERPRO runtime catalog."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date
from http.client import HTTPConnection
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from apps.core.tenancy import tenant_context
from apps.integrations import allowlist
from apps.integrations.call_contracts import AdmissionDeniedError, CallRequest
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_protocol import begin_io, claim, reserve
from apps.integrations.serpro import sitfis_qualification
from apps.integrations.serpro.codec import Parties, build_request
from apps.integrations.serpro.sitfis_qualification import _fake_origin
from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import OperacaoNaoPermitida, ProviderRequest
from apps.obligations.models import FederalConsultation
from apps.tenants.models import Tenant
from tests.fakes.provider import Reply, ScriptedProvider, permitted_send
from tests.fakes.serpro import FakeSerpro
from tests.integrations.serpro_support import CLIENT, CONTRATANTE, FIRM
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

__all__ = ["clock", "world"]

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


def _post(origin: str, path: str, body: dict[str, Any]) -> tuple[int, bytes]:
    connection = HTTPConnection(origin.removeprefix("http://"), timeout=5)
    try:
        connection.request("POST", path, json.dumps(body).encode())
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


@pytest.mark.parametrize("operation", SERVICES)
def test_built_body_matches_official_example(operation: str) -> None:
    expected = _expected(operation)
    pedido = expected["pedidoDados"]
    example_data = pedido["dados"]
    data = json.loads(example_data) if example_data else ""
    if example_data:
        pedido["dados"] = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    actual = build_request(operation, PARTIES, data)
    assert json.loads(actual.body) == expected
    if not example_data:
        assert b'"dados":""' in actual.body


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
    assert "environment" in signature(build_request).parameters
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
        assert date.fromisoformat(contract.read_date) == date.fromisoformat(
            vector["read_date"]
        )
        if not verified:
            assert contract.trial_page == vector["trial_page"]
            assert contract.production_schema == vector["production_schema"]
            assert contract.version_source == vector["version_source"]


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


@pytest.mark.django_db(transaction=True)
def test_trial_sitfis_binds_the_trial_bytes_to_the_journal(
    settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings.MEDIA_ROOT = tmp_path
    settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL = {"serpro": "10.00"}
    settings.USAGE_UNIT_PRICES_BRL = sitfis_qualification.SYNTHETIC_PRICES
    monkeypatch.setenv("SERPRO_TRIAL_BEARER", "synthetic-wire-test")

    def no_wait(_seconds: float) -> None:
        pytest.fail("the loopback trial returns a protocol immediately")

    # Keep production transport and protocol code; substitute only the network origin.
    monkeypatch.setattr(sitfis_qualification, "time", SimpleNamespace(sleep=no_wait))
    with ScriptedProvider((termo_reply(), protocol_reply(0), report_reply())) as peer:
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
    for request, call in zip((protocol, emission), calls, strict=True):
        assert isinstance(call, dict)
        assert json.loads(request.body)["pedidoDados"]["versaoSistema"] == "1.0"
        assert call["request_digest"] == hashlib.sha256(request.body).hexdigest()
    assert json.loads(protocol.body)["pedidoDados"]["dados"] == ""
    # The synthetic trial created exactly one tenant and one consultation.
    tenant = Tenant.objects.get(slug__startswith="pq-serpro-")
    with tenant_context(tenant.pk):
        consultation = FederalConsultation.objects.get()
        assert consultation.scope_digest == hashlib.sha256(protocol.body).hexdigest()


@pytest.mark.django_db(transaction=True)
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
