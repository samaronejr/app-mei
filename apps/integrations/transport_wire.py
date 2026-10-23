"""Private bounded wire primitive shared only by provider and broker clients."""

from contextlib import closing
from dataclasses import dataclass
from http import HTTPStatus
from time import monotonic
from typing import TYPE_CHECKING, Final

from requests import Request, Response
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException, SSLError, Timeout
from urllib3.exceptions import HTTPError

from apps.delivery.privacy import delivery_secrets
from apps.integrations.transport_types import ProviderResponse, TransportFailure

if TYPE_CHECKING:
    from urllib3.response import BaseHTTPResponse

CONNECT_TIMEOUT: Final = 5.0
READ_TIMEOUT: Final = 30.0
SITFIS_TIMEOUT: Final = 60.0
XML_LIMIT: Final = 5 * 1024 * 1024
PDF_LIMIT: Final = 10 * 1024 * 1024
SAFE_HEADERS: Final = frozenset(
    {"content-type", "x-max-version", "x-total-count", "retry-after"}
)


@dataclass(frozen=True, slots=True)
class WireLimits:
    """Limits are selected by the operation, not by untrusted response headers."""

    size: int = XML_LIMIT
    read: float = READ_TIMEOUT
    ca_file: str = ""


def _consume(response: Response, limits: WireLimits) -> ProviderResponse:
    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
        msg = "codificacao_nao_permitida"
        raise TransportFailure(msg, response.status_code)
    length = response.headers.get("Content-Length")
    if length is not None:
        if not length.isascii() or not length.isdigit():
            msg = "resposta_invalida"
            raise TransportFailure(msg, response.status_code)
        if int(length) > limits.size:
            msg = "resposta_excede_limite"
            raise TransportFailure(msg, response.status_code)
    # read1 performs at most one buffered socket read. Unlike read(chunk_size),
    # a peer dripping bytes cannot hold us until a whole chunk has accumulated.
    raw: BaseHTTPResponse = response.raw
    deadline = monotonic() + limits.read
    data = bytearray()
    while True:
        part = raw.read1(min(65536, limits.size + 1 - len(data)), decode_content=False)
        if monotonic() > deadline:
            msg = "tempo_limite"
            raise TransportFailure(msg)
        if not part:
            break
        data.extend(part)
        if len(data) > limits.size:
            msg = "resposta_excede_limite"
            raise TransportFailure(msg, response.status_code)
    return ProviderResponse(
        response.status_code,
        bytes(data),
        tuple(
            (k.lower(), v)
            for k, v in response.headers.items()
            if k.lower() in SAFE_HEADERS
        ),
    )


def _exchange(
    request: Request, limits: WireLimits, *, redirect: bool = False
) -> ProviderResponse | str:
    """Exactly one transmission; redirects return a transient Location, never bytes."""
    # No Session: no cookies, netrc, environment proxies/CA overrides or redirect
    # preparation. HTTPAdapter disables redirects and retries explicitly.
    with delivery_secrets(
        str(request.url), *(str(value) for value in (request.headers or {}).values())
    ):
        try:
            with (
                closing(HTTPAdapter(max_retries=0)) as adapter,
                adapter.send(
                    request.prepare(),
                    stream=True,
                    timeout=(CONNECT_TIMEOUT, limits.read),
                    verify=limits.ca_file or True,
                    cert=None,
                    proxies={},
                ) as response,
            ):
                if (
                    HTTPStatus.MULTIPLE_CHOICES
                    <= response.status_code
                    < HTTPStatus.BAD_REQUEST
                ):
                    if redirect and response.status_code == HTTPStatus.FOUND:
                        return response.headers.get("Location", "")
                    # SERPRO 304 is a protocol result, not a redirect.
                    if response.status_code != HTTPStatus.NOT_MODIFIED:
                        msg = "redirecionamento_nao_permitido"
                        raise TransportFailure(msg, response.status_code)
                return _consume(response, limits)
        except SSLError:
            msg = "tls_invalido"
            raise TransportFailure(msg) from None
        except Timeout:
            msg = "tempo_limite"
            raise TransportFailure(msg) from None
        except (RequestException, HTTPError, OSError):
            msg = "conexao_interrompida"
            raise TransportFailure(msg) from None
