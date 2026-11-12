"""Private bounded wire primitive shared only by provider and broker clients."""

import socket
import threading
from contextlib import closing, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from http import HTTPStatus
from time import monotonic
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from requests import PreparedRequest, Request, Response
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException, SSLError, Timeout
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import HTTPError, ReadTimeoutError

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


class _Watchdog:
    """Bounds one whole exchange: response headers and body share the read budget.

    The budget starts when the connection is established. When it is spent the
    socket is shut down, which ends any blocked read (a peer dripping header or
    body bytes resets per-receive socket timeouts, but not this deadline).
    """

    def __init__(self, budget: float) -> None:
        self._budget = budget
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._timer: threading.Timer | None = None
        self.deadline = float("inf")
        self.expired = False

    def attach(self, sock: socket.socket) -> None:
        with self._lock:
            self._sock = sock
            self.deadline = monotonic() + self._budget
            self._timer = threading.Timer(self._budget, self._expire)
            self._timer.daemon = True
            self._timer.start()

    def _expire(self) -> None:
        with self._lock:
            self.expired = True
            sock = self._sock
        if sock is not None:
            # The plain socket's shutdown, so a TLS object is never altered while
            # the reading thread is inside it.
            with suppress(OSError):
                socket.socket.shutdown(sock, socket.SHUT_RDWR)

    def cancel(self) -> None:
        with self._lock:
            self._sock = None
            timer = self._timer
        if timer is not None:
            timer.cancel()


_EXCHANGE: ContextVar[_Watchdog | None] = ContextVar("provider_exchange", default=None)


def _watch(sock: socket.socket | None) -> None:
    watchdog = _EXCHANGE.get()
    if watchdog is not None and sock is not None:
        watchdog.attach(sock)


class _WatchedHTTPConnection(HTTPConnection):
    def connect(self) -> None:
        super().connect()
        _watch(self.sock)


class _WatchedHTTPSConnection(HTTPSConnection):
    def connect(self) -> None:
        super().connect()
        _watch(self.sock)


class _WatchedHTTPPool(HTTPConnectionPool):
    ConnectionCls = _WatchedHTTPConnection


class _WatchedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _WatchedHTTPSConnection


class _WatchedAdapter(HTTPAdapter):
    """HTTPAdapter whose connections register their socket with the exchange."""

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        super().init_poolmanager(*args, **kwargs)
        pools: dict[str, type[HTTPConnectionPool]] = {
            "http": _WatchedHTTPPool,
            "https": _WatchedHTTPSPool,
        }
        self.poolmanager.pool_classes_by_scheme = pools


def _check_declared(response: Response, limits: WireLimits) -> None:
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


def _consume(
    response: Response, limits: WireLimits, deadline: float
) -> ProviderResponse:
    _check_declared(response, limits)
    # read1 performs at most one buffered socket read. Unlike read(chunk_size),
    # a peer dripping bytes cannot hold us until a whole chunk has accumulated.
    # A read that blocks past the deadline is ended by the exchange watchdog.
    raw: BaseHTTPResponse = response.raw
    data = bytearray()
    while True:
        if deadline - monotonic() <= 0:
            msg = "tempo_limite"
            raise TransportFailure(msg)
        part = raw.read1(min(65536, limits.size + 1 - len(data)), decode_content=False)
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


def _send(
    prepared: PreparedRequest,
    limits: WireLimits,
    watchdog: _Watchdog,
    *,
    redirect: bool,
) -> ProviderResponse | str:
    # No Session: no cookies, netrc, environment proxies/CA overrides or redirect
    # preparation. The adapter disables redirects and retries explicitly.
    with (
        closing(_WatchedAdapter(max_retries=0)) as adapter,
        adapter.send(
            prepared,
            stream=True,
            timeout=(CONNECT_TIMEOUT, limits.read),
            verify=limits.ca_file or True,
            cert=None,
            proxies={},
        ) as response,
    ):
        if HTTPStatus.MULTIPLE_CHOICES <= response.status_code < HTTPStatus.BAD_REQUEST:
            if redirect and response.status_code == HTTPStatus.FOUND:
                return response.headers.get("Location", "")
            # SERPRO 304 is a protocol result, not a redirect.
            if response.status_code != HTTPStatus.NOT_MODIFIED:
                msg = "redirecionamento_nao_permitido"
                raise TransportFailure(msg, response.status_code)
        return _consume(response, limits, watchdog.deadline)


def _exchange(
    request: Request, limits: WireLimits, *, redirect: bool = False
) -> ProviderResponse | str:
    """Exactly one transmission; redirects return a transient Location, never bytes.

    Once the read budget is spent every outcome is a timeout, including a read
    that the watchdog ended with a closed socket or a truncated body.
    """
    prepared = request.prepare()
    watchdog = _Watchdog(limits.read)
    token = _EXCHANGE.set(watchdog)
    # urllib3 logs the request line (path and query) while this call runs.
    with delivery_secrets(
        str(prepared.url),
        str(prepared.path_url),
        urlsplit(str(prepared.url)).query,
        *(str(value) for value in (request.headers or {}).values()),
    ):
        try:
            outcome = _send(prepared, limits, watchdog, redirect=redirect)
        except TransportFailure:
            if not watchdog.expired:
                raise
            code = "tempo_limite"
        except SSLError:
            code = "tls_invalido"
        except (Timeout, ReadTimeoutError, TimeoutError):
            code = "tempo_limite"
        except (RequestException, HTTPError, OSError):
            code = "conexao_interrompida"
        else:
            if not watchdog.expired:
                return outcome
            code = "tempo_limite"
        finally:
            watchdog.cancel()
            _EXCHANGE.reset(token)
    raise TransportFailure("tempo_limite" if watchdog.expired else code)
