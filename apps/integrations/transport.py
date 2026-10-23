"""Provider-only transport: closed operation routes, verified TLS, one PDF hop."""

from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

from requests import Request

from apps.core.tasks import assert_no_transaction
from apps.integrations.allowlist import resolve
from apps.integrations.call_dispatch import open_wire
from apps.integrations.transport_types import (
    Environment,
    OperacaoNaoPermitida,
    Provider,
    ProviderRequest,
    ProviderResponse,
    TransportFailure,
)
from apps.integrations.transport_wire import (
    PDF_LIMIT,
    READ_TIMEOUT,
    SITFIS_TIMEOUT,
    XML_LIMIT,
    WireLimits,
    _exchange,
)

FOCUS_PDF_HOSTS: Final = frozenset({"focusnfe.s3.sa-east-1.amazonaws.com"})
ORIGINS: Final = {
    (
        "serpro",
        "producao",
    ): "https://gateway.apiserpro.serpro.gov.br/integra-contador/v1",
    (
        "serpro",
        "trial",
    ): "https://gateway.apiserpro.serpro.gov.br/integra-contador-trial/v1",
    ("focus", "producao"): "https://api.focusnfe.com.br",
    ("focus", "homologacao"): "https://homologacao.focusnfe.com.br",
}


def _local_origin(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname == "127.0.0.1"
        and parsed.username is None
        and parsed.password is None
        and parsed.port is not None
        and parsed.path == ""
        and not parsed.query
        and not parsed.fragment
        and value == f"{parsed.scheme}://127.0.0.1:{parsed.port}"
    )


def _pdf_location(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and parsed.netloc in FOCUS_PDF_HOSTS
            and parsed.hostname in FOCUS_PDF_HOSTS
            and parsed.username is None
            and parsed.port is None
            and not parsed.fragment
            and not any(
                char.isspace() or ord(char) < ord(" ") or char == "\\" for char in value
            )
        )
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class ProviderTransport:
    """Sends only inside the AXRM-009 dispatch that transmit() opened for it.

    Fake endpoints must be explicit loopback origins. Real origins cannot be
    overridden; a custom CA bundle adds trust roots, never disables verification.
    """

    provider: Provider
    environment: Environment
    fake_origin: str = ""
    ca_file: str = field(default="", repr=False)

    def _origin(self) -> str:
        if self.environment == "fake":
            try:
                valid = _local_origin(self.fake_origin)
            except ValueError:
                valid = False
            if not valid:
                raise OperacaoNaoPermitida
            return self.fake_origin
        if self.fake_origin:
            raise OperacaoNaoPermitida
        origin = ORIGINS.get((self.provider, self.environment))
        if origin is None:
            raise OperacaoNaoPermitida
        return origin

    def send(self, operation: str, request: ProviderRequest) -> ProviderResponse:
        """Resolve before touching the network; discard every credential on PDF hop."""
        assert_no_transaction()
        route, path = resolve(self.provider, operation, request)
        origin = self._origin()
        open_wire(self.provider, self.environment, operation)
        limits = WireLimits(
            PDF_LIMIT if route.media == "pdf" else XML_LIMIT,
            SITFIS_TIMEOUT if operation == "SP-SIT-R" else READ_TIMEOUT,
            self.ca_file,
        )
        headers = dict(request.headers)
        headers["Accept-Encoding"] = "identity"
        headers.setdefault("Content-Type", "application/json")
        response = _exchange(
            Request(route.method, origin + path, headers=headers, data=request.body),
            limits,
            redirect=operation == "FC-PDF",
        )
        if isinstance(response, str):
            if not _pdf_location(response):
                msg = "redirecionamento_nao_permitido"
                raise TransportFailure(msg, 302)
            # A brand new request/adapter: no Authorization, JWT, cookies, Referer
            # or environment/netrc credentials survive the one allowed hop.
            downloaded = _exchange(
                Request("GET", response, headers={"Accept-Encoding": "identity"}),
                limits,
            )
            if isinstance(downloaded, str):
                msg = "redirecionamento_nao_permitido"
                raise TransportFailure(msg, 302)
            return downloaded
        return response
