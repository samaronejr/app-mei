"""Private bounded wire primitive shared only by provider and broker clients."""

import re
import socket
import ssl
import threading
from contextlib import closing, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from http import HTTPStatus
from http.client import HTTPResponse
from io import BufferedReader, BytesIO
from time import monotonic
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import urlsplit

from requests import PreparedRequest, Request, Response
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException, SSLError, Timeout
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import HTTPError, ReadTimeoutError

from apps.delivery.privacy import delivery_secrets
from apps.integrations.transport_headers import (
    FRAMING_LIMIT,
    TOKEN,
    read_fields,
    read_response_head,
)
from apps.integrations.transport_types import ProviderResponse, TransportFailure

if TYPE_CHECKING:
    from typing import Protocol

    from urllib3.response import BaseHTTPResponse

    class _TLSReadPolicy(Protocol):
        # SSLSocket's per-socket read flag in the pinned CPython.
        suppress_ragged_eofs: bool


CONNECT_TIMEOUT: Final = 5.0
READ_TIMEOUT: Final = 30.0
SITFIS_TIMEOUT: Final = 60.0
XML_LIMIT: Final = 5 * 1024 * 1024
PDF_LIMIT: Final = 10 * 1024 * 1024
SAFE_HEADERS: Final = frozenset(
    {"content-type", "x-max-version", "x-total-count", "retry-after", "etag", "expires"}
)
_QUOTED: Final = (
    rb'"(?:[\t\x20\x21\x23-\x5b\x5d-\x7e\x80-\xff]|\\[\t\x20-\x7e\x80-\xff])*"'
)
_CHUNK_LINE: Final = re.compile(
    rb"([0-9a-fA-F]+)(?:[ \t]*;[ \t]*"
    + TOKEN
    + rb"(?:[ \t]*=[ \t]*(?:"
    + TOKEN
    + rb"|"
    + _QUOTED
    + rb"))?)*\r\n"
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

    def __init__(self, limits: WireLimits) -> None:
        self.limits = limits
        self._budget = limits.read
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


_EXCHANGE: ContextVar[_Watchdog] = ContextVar("provider_exchange")


def _watch(sock: socket.socket | None) -> None:
    watchdog = _EXCHANGE.get(None)
    if watchdog is not None and sock is not None:
        watchdog.attach(sock)


class _StrictHTTPResponse(HTTPResponse):
    """Initialize the streaming reader only from unambiguous response fields."""

    def begin(self) -> None:
        if self.headers is not None:
            return
        stream = self.fp
        head = read_response_head(stream, _EXCHANGE.get().limits.size)
        # http.client's first int(length), and later urllib3's, now see only
        # the single bounded value. Keep all buffered body bytes for read1.
        replay: BufferedReader = BufferedReader(BytesIO(head))
        with replay:
            try:
                self.fp = replay
                super().begin()
            finally:
                self.fp = stream

    def _get_chunk_left(self) -> int | None:
        if self.chunk_left:
            return self.chunk_left
        if self.chunk_left == 0 and self.fp.read(2) != b"\r\n":
            msg = "resposta_invalida"
            raise TransportFailure(msg, self.status)
        line = self.fp.readline(FRAMING_LIMIT + 1)
        match = _CHUNK_LINE.fullmatch(line) if len(line) <= FRAMING_LIMIT else None
        if match is None:
            msg = "resposta_invalida"
            raise TransportFailure(msg, self.status)
        size = match.group(1).lstrip(b"0").lower() or b"0"
        maximum = format(_EXCHANGE.get().limits.size, "x").encode("ascii")
        if len(size) > len(maximum) or (len(size) == len(maximum) and size > maximum):
            msg = "resposta_excede_limite"
            raise TransportFailure(msg, self.status)
        self.chunk_left = int(size, 16)
        if self.chunk_left:
            return self.chunk_left
        # Apply the same field/fold rules; trailer metadata never overrides
        # the response headers or its already established framing.
        read_fields(self.fp, self.status)
        self.close()
        self.chunk_left = None
        return None


class _WatchedHTTPConnection(HTTPConnection):
    response_class = _StrictHTTPResponse

    def connect(self) -> None:
        super().connect()
        _watch(self.sock)


class _WatchedHTTPSConnection(HTTPSConnection):
    response_class = _StrictHTTPResponse

    def connect(self) -> None:
        super().connect()
        if not isinstance(self.sock, ssl.SSLSocket):
            msg = "tls_invalido"
            raise TransportFailure(msg)
        # SSLSocket.read normally turns an unexpected EOF into empty bytes,
        # indistinguishable from a close_notify on a close-delimited response.
        cast("_TLSReadPolicy", self.sock).suppress_ragged_eofs = False
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


def _consume(
    response: Response, limits: WireLimits, deadline: float
) -> ProviderResponse:
    # The validated head replays only codings other than identity.
    if "Content-Encoding" in response.headers:
        msg = "codificacao_nao_permitida"
        raise TransportFailure(msg, response.status_code)
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
    watchdog = _Watchdog(limits)
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
