"""Wire-level acceptance: no real providers and no database required."""

import json
import socket
import ssl
import threading
import time
from pathlib import Path
from typing import Self

import pytest

from apps.integrations import transport, transport_wire
from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import (
    OperacaoNaoPermitida,
    ProviderRequest,
    TransportFailure,
)
from tests.fakes.focus import FakeFocus
from tests.fakes.provider import Reply, permitted_send
from tests.fakes.serpro import FakeSerpro

FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers"
KEY = "0" * 44


def serpro_request(
    service: str = "PAGAMENTOS71", system: str = "PAGTOWEB"
) -> ProviderRequest:
    return ProviderRequest(
        body=json.dumps(
            {
                "pedidoDados": {
                    "idSistema": system,
                    "idServico": service,
                    "versaoSistema": "1.0",
                }
            }
        ).encode()
    )


def test_happy_json_is_parsed_after_bounded_wire_read() -> None:
    with FakeSerpro(
        (Reply(body=(FIXTURES / "serpro/pagamentos.json").read_bytes()),)
    ) as peer:
        result = permitted_send(
            ProviderTransport("serpro", "fake", peer.origin), "SP-PAG", serpro_request()
        )
        value = result.json()
        assert isinstance(value, dict)
        assert value["status"] == 200
        assert value["dados"] == '{"pagamentos":[]}'
        assert len(peer.requests) == 1


@pytest.mark.parametrize(
    "status", [200, 202, 204, 304, 400, 401, 403, 404, 429, 500, 503, 504]
)
def test_scripted_status_is_returned_without_retry(status: int) -> None:
    with FakeSerpro((Reply(status=status, body=b""),)) as peer:
        response = permitted_send(
            ProviderTransport("serpro", "fake", peer.origin), "SP-PAG", serpro_request()
        )
        assert response.status == status
        assert len(peer.requests) == 1


@pytest.mark.parametrize("declared", [True, False])
def test_six_mib_is_refused_before_parse(declared: bool) -> None:
    with FakeFocus(
        (Reply(body=b"x" * (6 * 1024 * 1024), declared_length=declared),)
    ) as peer:
        with pytest.raises(TransportFailure) as caught:
            permitted_send(
                ProviderTransport("focus", "fake", peer.origin),
                "FC-NFE",
                ProviderRequest(),
            )
        result = caught.value.result()
        assert (result.state, result.provider_code) == (
            "falhou",
            "resposta_excede_limite",
        )
        assert result.http_status == 200
        assert len(peer.requests) == 1


def test_declared_oversize_is_rejected_before_any_body_read() -> None:
    # The peer declares 6 MiB but sends no body: reading it would fail framing.
    with FakeFocus(
        (
            Reply(
                body=b"",
                declared_length=False,
                headers=(("Content-Length", str(6 * 1024 * 1024)),),
            ),
        )
    ) as peer:
        with pytest.raises(TransportFailure) as caught:
            permitted_send(
                ProviderTransport("focus", "fake", peer.origin),
                "FC-NFE",
                ProviderRequest(),
            )
        assert caught.value.code == "resposta_excede_limite"


@pytest.mark.parametrize("size", [10 * 1024 * 1024, 10 * 1024 * 1024 + 1])
def test_pdf_uses_its_own_size_cap(size: int) -> None:
    with FakeFocus((Reply(body=b"p" * size, declared_length=False),)) as peer:
        client = ProviderTransport("focus", "fake", peer.origin)
        if size > 10 * 1024 * 1024:
            with pytest.raises(TransportFailure, match="resposta_excede_limite"):
                permitted_send(client, "FC-PDF", ProviderRequest(subject=KEY))
        else:
            assert (
                len(permitted_send(client, "FC-PDF", ProviderRequest(subject=KEY)).body)
                == size
            )


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_ordinary_redirects_never_reach_second_server(status: int) -> None:
    with (
        FakeFocus(()) as target,
        FakeFocus(
            (
                Reply(
                    status=status,
                    headers=(("Location", target.origin),),
                    body=b"x" * 100000,
                ),
            )
        ) as source,
    ):
        with pytest.raises(TransportFailure, match="redirecionamento_nao_permitido"):
            permitted_send(
                ProviderTransport("focus", "fake", source.origin),
                "FC-NFE",
                ProviderRequest(),
            )
        assert not target.requests


@pytest.mark.parametrize(
    "code",
    [
        "SP-X",
        "FC-X",
        "FC-CONC",
        "SP-AUTH",
        "PGMEI/ATUBENEFICIO23",
        "PGMEI/DIVIDAATIVA24",
        "/Declarar",
        "TRANSDECLARACAO11",
        "PARCMEI/GERARDAS22",
        "REGIMEAPURACAO/EFETUAROPCAOREGIME101",
        "PNRCONTADOR/SOLICRENUNCIA262",
        "confirmacao",
        "desconhecimento",
        "nao_realizada",
        "POST /v2/nfe",
        "POST /v2/nfse",
        "POST /v2/nfsen",
        "importar_nfe",
        "ECONF",
        "CT-e desacordo",
    ],
)
def test_every_excluded_operation_is_refused_without_network(code: str) -> None:
    with FakeFocus(()) as peer:
        with pytest.raises(OperacaoNaoPermitida):
            permitted_send(
                ProviderTransport("focus", "fake", peer.origin), code, ProviderRequest()
            )
        assert not peer.requests


@pytest.mark.parametrize(
    "service", ["ATUBENEFICIO23", "DIVIDAATIVA24", "GERARDASPDF21"]
)
def test_serpro_alias_cannot_smuggle_a_different_service(service: str) -> None:
    with FakeSerpro(()) as peer:
        with pytest.raises(OperacaoNaoPermitida):
            permitted_send(
                ProviderTransport("serpro", "fake", peer.origin),
                "SP-PAG",
                serpro_request(service, "PGMEI"),
            )
        assert not peer.requests


@pytest.mark.parametrize("event", ["confirmacao", "desconhecimento", "nao_realizada"])
def test_ciencia_route_cannot_submit_conclusive_events(event: str) -> None:
    with FakeFocus(()) as peer:
        with pytest.raises(OperacaoNaoPermitida):
            permitted_send(
                ProviderTransport("focus", "fake", peer.origin),
                "FC-CIE",
                ProviderRequest(subject=KEY, body=json.dumps({"tipo": event}).encode()),
            )
        assert not peer.requests


@pytest.mark.parametrize(
    "subject", ["../nfe", "%2e%2e", "//evil.invalid", KEY + "?token=x", "\uff11" * 44]
)
def test_path_interpolation_cannot_escape_operation(subject: str) -> None:
    with FakeFocus(()) as peer:
        with pytest.raises(OperacaoNaoPermitida):
            permitted_send(
                ProviderTransport("focus", "fake", peer.origin),
                "FC-NFE",
                ProviderRequest(variant="xml", subject=subject),
            )
        assert not peer.requests


@pytest.mark.parametrize(
    "location",
    [
        "http://focusnfe.s3.sa-east-1.amazonaws.com/file",
        "https://focusnfe.s3.sa-east-1.amazonaws.com.evil.invalid/file",
        "https://evil.s3.sa-east-1.amazonaws.com/file",
        "https://focusnfe.s3.sa-east-1.amazonaws.com@127.0.0.1/file",
        "https://127.0.0.1/file",
        "//127.0.0.1/file",
        "https://focusnfe.s3.sa-east-1.amazonaws.com:443/file",
        "https://focusnfe.s3.sa-east-1.amazonaws.com\\@evil.invalid/file",
    ],
)
def test_pdf_redirect_host_bypass_is_refused(location: str) -> None:
    with FakeFocus((Reply(status=302, headers=(("Location", location),)),)) as peer:
        with pytest.raises(TransportFailure, match="redirecionamento_nao_permitido"):
            permitted_send(
                ProviderTransport("focus", "fake", peer.origin),
                "FC-PDF",
                ProviderRequest(subject=KEY),
            )
        assert len(peer.requests) == 1


def test_connection_reset_after_request_is_uncertain_not_retried() -> None:
    with FakeSerpro((Reply(reset=True),)) as peer:
        with pytest.raises(TransportFailure) as caught:
            permitted_send(
                ProviderTransport("serpro", "fake", peer.origin),
                "SP-PAG",
                serpro_request(),
            )
        assert caught.value.result().state == "incerta"
        assert len(peer.requests) == 1


def test_slow_peer_has_a_finite_socket_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport, "READ_TIMEOUT", 0.05)
    with FakeFocus((Reply(held=True),)) as peer:
        with pytest.raises(TransportFailure, match="tempo_limite"):
            permitted_send(
                ProviderTransport("focus", "fake", peer.origin),
                "FC-NFE",
                ProviderRequest(),
            )
        assert peer.received.is_set()


def test_body_budget_bounds_every_blocking_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a 1 s body budget and a peer that drips one byte every 0.8 s, four
    # times, and then holds the connection open with the body incomplete.
    # Elapsed time is the behaviour under test, so this test measures it.
    monkeypatch.setattr(transport, "READ_TIMEOUT", 1.0)
    drip = Reply(body=b"x" * 8, chunks=(b"x",) * 4, gap=0.8, hold_open=True)
    with FakeFocus((drip,)) as peer:
        # When the body is read.
        started = time.monotonic()
        with pytest.raises(TransportFailure) as caught:
            permitted_send(
                ProviderTransport("focus", "fake", peer.origin),
                "FC-NFE",
                ProviderRequest(),
            )
        elapsed = time.monotonic() - started
    # Then the read stops at the budget (about 1.0 s), not one full socket
    # timeout after the last byte (about 3.4 s); the bound leaves 1.5 s headroom.
    assert caught.value.code == "tempo_limite"
    assert elapsed < 2.5


def test_production_budget_stops_the_reviewed_real_drip() -> None:
    # Given the unmodified 30 s read budget and a real peer that sends one body
    # byte at 0, 20 and 40 s and keeps the body incomplete (review finding B3).
    drip = Reply(body=b"x" * 8, chunks=(b"x", b"x", b"x"), gap=20.0, hold_open=True)
    with FakeFocus((drip,)) as peer:
        # When the body is read.
        started = time.monotonic()
        with pytest.raises(TransportFailure) as caught:
            permitted_send(
                ProviderTransport("focus", "fake", peer.origin),
                "FC-NFE",
                ProviderRequest(),
            )
        elapsed = time.monotonic() - started
    # Then it aborts at the budget plus a small margin (it took 40 s before).
    assert caught.value.code == "tempo_limite"
    assert 29.0 <= elapsed <= 31.0


class HeaderDrip:
    """A raw peer that sends a status line, then one header byte per `gap`."""

    def __init__(
        self, gap: float, count: int, tls: ssl.SSLContext | None = None
    ) -> None:
        listener = socket.create_server(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        self._listener = (
            listener if tls is None else tls.wrap_socket(listener, server_side=True)
        )
        self.origin = f"{'https' if tls else 'http'}://127.0.0.1:{port}"
        self._gap = gap
        self._count = count
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(5)

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
            with conn:
                conn.recv(65536)
                conn.sendall(b"HTTP/1.1 200 OK\r\nX-Pad: ")
                for _ in range(self._count):
                    if self._stop.wait(self._gap):
                        return
                    conn.sendall(b"a")
                conn.sendall(b"\r\nContent-Length: 2\r\n\r\n{}")
        except OSError:
            return


def timed_header_drip(peer: HeaderDrip, ca_file: str = "") -> tuple[str, float]:
    started = time.monotonic()
    code = "ok"
    try:
        permitted_send(
            ProviderTransport("focus", "fake", peer.origin, ca_file),
            "FC-NFE",
            ProviderRequest(),
        )
    except TransportFailure as error:
        code = error.code
    return code, time.monotonic() - started


def test_header_drip_stops_at_the_read_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a 1 s read budget and a peer that drips one response-header byte
    # every 0.25 s for 10 s (each byte would reset a per-receive timeout).
    monkeypatch.setattr(transport, "READ_TIMEOUT", 1.0)
    with HeaderDrip(gap=0.25, count=40) as peer:
        # When the reply is read.
        code, elapsed = timed_header_drip(peer)
    # Then the exchange stops at the budget (about 1 s), not after the drip.
    assert code == "tempo_limite"
    assert elapsed < 4.0


def test_production_budget_stops_a_real_header_drip() -> None:
    # Given the unmodified 30 s read budget and a real peer that drips one
    # response-header byte per second for 60 s (review 2 finding B2: 60.007 s).
    with HeaderDrip(gap=1.0, count=60) as peer:
        # When the reply is read.
        code, elapsed = timed_header_drip(peer)
    # Then it aborts at the budget plus a small margin.
    assert code == "tempo_limite"
    assert 29.0 <= elapsed <= 31.0


def test_exhausted_budget_stops_before_the_next_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a body that needs a second read, and a clock that passes the 30 s
    # budget between the first and second read.
    readings = iter([0.0, 0.0])
    monkeypatch.setattr(transport_wire, "monotonic", lambda: next(readings, 31.0))
    partial = Reply(body=b"x" * 8, chunks=(b"xx",), hold_open=True)
    # When the body is read.
    started = time.monotonic()
    with (
        FakeFocus((partial,)) as peer,
        pytest.raises(TransportFailure) as caught,
    ):
        permitted_send(
            ProviderTransport("focus", "fake", peer.origin),
            "FC-NFE",
            ProviderRequest(),
        )
    # Then the read stops as a timeout instead of waiting again (a blocked read
    # would wait for the 30 s watchdog).
    assert caught.value.code == "tempo_limite"
    assert time.monotonic() - started < 10.0


def test_compressed_body_is_refused_before_decompression() -> None:
    with (
        FakeFocus(
            (Reply(body=b"compressed", headers=(("Content-Encoding", "gzip"),)),)
        ) as peer,
        pytest.raises(TransportFailure, match="codificacao_nao_permitida"),
    ):
        permitted_send(
            ProviderTransport("focus", "fake", peer.origin), "FC-NFE", ProviderRequest()
        )


@pytest.mark.parametrize(
    "body", [b"{", b"<malformed", b"\xff", b"NaN", b'{"a":1,"a":2}']
)
def test_malformed_json_is_not_echoed_in_error(body: bytes) -> None:
    with FakeFocus((Reply(body=body),)) as peer:
        response = permitted_send(
            ProviderTransport("focus", "fake", peer.origin), "FC-NFE", ProviderRequest()
        )
        with pytest.raises(TransportFailure, match="resposta_invalida"):
            response.json()
