"""Closed catalog positive controls and independent boundary attack witnesses."""

import json

import pytest

from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import (
    Environment,
    OperacaoNaoPermitida,
    ProviderRequest,
)
from tests.fakes.focus import FakeFocus
from tests.fakes.provider import Reply, permitted_send
from tests.fakes.serpro import FakeSerpro
from tests.integrations.test_transport import KEY


@pytest.mark.parametrize(
    ("code", "wire_request", "method", "path"),
    [
        ("FC-NFE", ProviderRequest(), "GET", "/v2/nfes_recebidas"),
        (
            "FC-NFE",
            ProviderRequest(variant="xml", subject=KEY),
            "GET",
            f"/v2/nfes_recebidas/{KEY}.xml",
        ),
        (
            "FC-NFE",
            ProviderRequest(variant="cce", subject=KEY),
            "GET",
            f"/v2/nfes_recebidas/{KEY}/carta_correcao.xml",
        ),
        ("FC-NFS", ProviderRequest(), "GET", "/v2/nfsens_recebidas"),
        (
            "FC-NFS",
            ProviderRequest(variant="xml", subject=KEY),
            "GET",
            f"/v2/nfsens_recebidas/{KEY}.xml",
        ),
        (
            "FC-CIE",
            ProviderRequest(subject=KEY, body=b'{"tipo":"ciencia"}'),
            "POST",
            f"/v2/nfes_recebidas/{KEY}/manifesto",
        ),
        (
            "FC-EMP",
            ProviderRequest(variant="read", subject="123"),
            "GET",
            "/v2/empresas/123",
        ),
        (
            "FC-EMP",
            ProviderRequest(variant="create", body=b'{"cnpj":"00000000000000"}'),
            "POST",
            "/v2/empresas",
        ),
        (
            "FC-EMP",
            ProviderRequest(
                variant="update",
                subject="123",
                body=b'{"habilita_nfe_recebidas":false}',
            ),
            "PUT",
            "/v2/empresas/123",
        ),
        (
            "FC-PDF",
            ProviderRequest(variant="nfse", subject=KEY),
            "GET",
            f"/v2/nfsens_recebidas/{KEY}.pdf",
        ),
    ],
)
def test_catalog_routes_have_wire_positive_controls(
    code: str,
    wire_request: ProviderRequest,
    method: str,
    path: str,
) -> None:
    with FakeFocus((Reply(),)) as peer:
        permitted_send(
            ProviderTransport("focus", "fake", peer.origin), code, wire_request
        )
        assert (peer.requests[0].method, peer.requests[0].path) == (method, path)


@pytest.mark.parametrize(
    "wire_request",
    [
        ProviderRequest(variant="../../nfe"),
        ProviderRequest(subject="unexpected"),
        ProviderRequest(body=b'{"unexpected":"GET body"}'),
        ProviderRequest(query=(("pendente_ciencia", "1"),)),
        ProviderRequest(query=(("cnpj", "1"), ("cnpj", "2"))),
        ProviderRequest(headers=(("Host", "evil.invalid"),)),
        ProviderRequest(headers=(("Authorization", "x"), ("authorization", "y"))),
        ProviderRequest(headers=(("Authorization", "x\r\nHost: evil.invalid"),)),
    ],
)
def test_request_boundary_rejects_bypass_before_network(
    wire_request: ProviderRequest,
) -> None:
    with FakeFocus(()) as peer:
        with pytest.raises(OperacaoNaoPermitida):
            permitted_send(
                ProviderTransport("focus", "fake", peer.origin), "FC-NFE", wire_request
            )
        assert not peer.requests


@pytest.mark.parametrize(
    "field",
    [
        "habilita_nfe",
        "habilita_nfse",
        "habilita_nfsen_producao",
        "habilita_nfce",
        "habilita_cte",
        "habilita_mdfe",
        "arquivo_certificado_base64",
        "senha_certificado",
        "habilita_manifestacao",
    ],
)
def test_company_configuration_cannot_enable_excluded_services(field: str) -> None:
    with FakeFocus(()) as peer:
        with pytest.raises(OperacaoNaoPermitida):
            permitted_send(
                ProviderTransport("focus", "fake", peer.origin),
                "FC-EMP",
                ProviderRequest(
                    variant="create", body=json.dumps({field: True}).encode()
                ),
            )
        assert not peer.requests


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:80",
        "http://127.0.0.1.evil.invalid:80",
        "http://user@127.0.0.1:80",
        "http://127.0.0.1:80/path",
        "http://127.0.0.1:80?token=x",
        "http://127.0.0.1:80#x",
        "http://[::1]:80",
        "http://2130706433:80",
    ],
)
def test_fake_configuration_is_strictly_loopback(origin: str) -> None:
    with pytest.raises(OperacaoNaoPermitida):
        permitted_send(
            ProviderTransport("focus", "fake", origin), "FC-NFE", ProviderRequest()
        )


@pytest.mark.parametrize("environment", ["producao", "homologacao"])
def test_real_environment_cannot_override_provider_host(
    environment: Environment,
) -> None:
    with FakeFocus(()) as peer:
        with pytest.raises(OperacaoNaoPermitida):
            permitted_send(
                ProviderTransport("focus", environment, peer.origin),
                "FC-NFE",
                ProviderRequest(),
            )
        assert not peer.requests


def test_provider_identity_cannot_be_swapped() -> None:
    with FakeFocus(()) as peer:
        with pytest.raises(OperacaoNaoPermitida):
            permitted_send(
                ProviderTransport("serpro", "fake", peer.origin),
                "FC-NFE",
                ProviderRequest(),
            )
        assert not peer.requests


def test_pgmei_benefit_update_is_refused_without_serpro_network() -> None:
    with FakeSerpro(()) as peer:
        with pytest.raises(OperacaoNaoPermitida):
            permitted_send(
                ProviderTransport("serpro", "fake", peer.origin),
                "PGMEI/ATUBENEFICIO23",
                ProviderRequest(),
            )
        assert not peer.requests


def test_duplicate_json_keys_cannot_disguise_a_conclusive_event() -> None:
    with FakeFocus(()) as peer:
        with pytest.raises(OperacaoNaoPermitida):
            permitted_send(
                ProviderTransport("focus", "fake", peer.origin),
                "FC-CIE",
                ProviderRequest(
                    subject=KEY, body=b'{"tipo":"confirmacao","tipo":"ciencia"}'
                ),
            )
        assert not peer.requests
