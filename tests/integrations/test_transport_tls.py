"""The real TLS stack and real redirected GET enforce the trust boundary."""

import socket
from pathlib import Path

import pytest

from apps.integrations.transport import ProviderTransport, _pdf_location
from apps.integrations.transport_types import ProviderRequest, TransportFailure
from tests.fakes.focus import FakeFocus
from tests.fakes.provider import Reply, permitted_send
from tests.fakes.serpro import FakeSerpro
from tests.integrations.test_transport import KEY, serpro_request
from tests.integrations.transport_tls import HOST, route_storage_host, tls_context


def test_pdf_redirect_host_predicate_rejects_untrusted_bucket() -> None:
    assert not _pdf_location("https://evil.s3.sa-east-1.amazonaws.com/file.pdf")
    assert _pdf_location(f"https://{HOST}/file.pdf")


def test_tls_rejects_untrusted_certificate_even_with_environment_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, ca = tls_context(tmp_path)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", ca)
    monkeypatch.setenv("CURL_CA_BUNDLE", ca)
    with FakeFocus((Reply(),), tls=context) as peer:
        with pytest.raises(TransportFailure, match="tls_invalido"):
            permitted_send(
                ProviderTransport("focus", "fake", peer.origin),
                "FC-NFE",
                ProviderRequest(),
            )
        assert not peer.requests


def test_verified_tls_positive_control(tmp_path: Path) -> None:
    context, ca = tls_context(tmp_path)
    with FakeFocus((Reply(),), tls=context) as peer:
        response = permitted_send(
            ProviderTransport("focus", "fake", peer.origin, ca),
            "FC-NFE",
            ProviderRequest(),
        )
        assert response.status == 200
        assert len(peer.requests) == 1


@pytest.mark.parametrize("second_status", [200, 302])
def test_focus_pdf_one_hop_drops_all_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    second_status: int,
) -> None:
    context, ca = tls_context(tmp_path)
    location = f"https://{HOST}/synthetic.pdf?X-Amz-Signature=synthetic-signature"
    with (
        FakeFocus(
            (
                Reply(
                    status=second_status,
                    body=b"%PDF-synthetic",
                    headers=(("Location", location),),
                ),
            ),
            tls=context,
        ) as target,
        FakeFocus(
            (
                Reply(
                    status=302,
                    headers=(
                        ("Location", location),
                        ("Set-Cookie", "token=synthetic-cookie"),
                    ),
                ),
            )
        ) as source,
    ):
        route_storage_host(monkeypatch, target.port)
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
        client = ProviderTransport("focus", "fake", source.origin, ca)
        request = ProviderRequest(
            subject=KEY,
            headers=(
                ("Authorization", "Basic synthetic-secret"),
                ("jwt_token", "synthetic-jwt"),
                ("autenticar_procurador_token", "synthetic-procurador"),
            ),
        )
        if second_status == 302:
            with pytest.raises(
                TransportFailure, match="redirecionamento_nao_permitido"
            ):
                permitted_send(client, "FC-PDF", request)
        else:
            result = permitted_send(client, "FC-PDF", request)
            assert result.body == b"%PDF-synthetic"
            assert "synthetic-signature" not in repr(result)
        assert len(source.requests) == len(target.requests) == 1
        headers = {key.lower(): value for key, value in target.requests[0].headers}
        assert (
            not {
                "authorization",
                "jwt_token",
                "autenticar_procurador_token",
                "cookie",
                "referer",
            }
            & headers.keys()
        )
        assert (
            dict(source.requests[0].headers)["Authorization"]
            == "Basic synthetic-secret"
        )


@pytest.mark.parametrize(
    ("operation", "service", "system", "read_timeout"),
    [
        ("SP-PAG", "PAGAMENTOS71", "PAGTOWEB", 30.0),
        ("SP-SIT-R", "RELATORIOSITFIS92", "SITFIS", 60.0),
    ],
)
def test_socket_is_configured_with_split_operation_timeouts(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    service: str,
    system: str,
    read_timeout: float,
) -> None:
    seen: list[float | None] = []
    original = socket.socket.settimeout

    def observe(sock: socket.socket, timeout: float | None) -> None:
        seen.append(timeout)
        original(sock, timeout)

    monkeypatch.setattr(socket.socket, "settimeout", observe)
    with FakeSerpro((Reply(),)) as peer:
        permitted_send(
            ProviderTransport("serpro", "fake", peer.origin),
            operation,
            serpro_request(service, system),
        )
    assert 5.0 in seen
    assert read_timeout in seen
