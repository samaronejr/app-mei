"""One Focus send per dispatch: request building, credentials, limits and outcomes.

Focus authenticates with HTTP Basic: the token is the user and the password is
blank (doc.focusnfe.com.br/reference/autenticacao). The token is a deployment
secret named by the connection's `secret_ref`, read from the process environment
for the one send and never stored, logged or journaled (INV-R7). The adapter sends
only the A6 allowlist (`apps.integrations.allowlist`), and the update builder
refuses any field outside the setup/disable set -- issuance switches and key
material (`arquivo_certificado_base64`, `senha_certificado`) never reach a request.

A company reply is parsed here, before the call is final: only an attributable,
well-formed company (same Focus id) is a success, and only allowlisted status
fields leave this module. Focus's own `token_*` fields stay in the transient
response object, which the caller discards with the adapter.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final, Literal

from django.utils import timezone

from apps.integrations.allowlist import ROUTES
from apps.integrations.call_contracts import CallResult
from apps.integrations.call_models import CallState
from apps.integrations.focus.rate_limit import TokenRateLimiter
from apps.integrations.transport_types import (
    JSONValue,
    OperacaoNaoPermitida,
    ProviderRequest,
    ProviderResponse,
    TransportFailure,
)

if TYPE_CHECKING:
    from apps.integrations.call_dispatch import Dispatch
    from apps.integrations.models import ProviderConnection
    from apps.integrations.transport import ProviderTransport

# A6 FC-EMP: the only company fields Axorim ever writes.
SETUP_FIELDS: Final = frozenset(
    {
        "habilita_filtro_mde_destinatario_empresa",
        "certificado_especifico",
        "habilita_nfsen_recebidas_producao",
        "habilita_nfsen_recebidas_homologacao",
        "habilita_manifestacao",
        "habilita_manifestacao_homologacao",
        "data_inicio_recebimento_nfe",
        "data_inicio_recebimento_nfsen",
    }
)
# Plan todo 19 "Must NOT do": issuance switches are never sent.
ISSUANCE_FLAGS: Final = frozenset(
    {
        "habilita_nfe",
        "habilita_nfse",
        "habilita_nfsen_producao",
        "habilita_nfce",
        "habilita_cte",
        "habilita_mdfe",
    }
)
# The status fields kept from a company reply: switches and start dates only.
SNAPSHOT_FIELDS: Final = tuple(sorted(SETUP_FIELDS | ISSUANCE_FLAGS))
FAKE_TOKEN: Final = "synthetic-focus"  # noqa: S105 - the fake environment's marker
RATE_LIMITED: Final = "limite_local_de_requisicoes"
MISSING_CREDENTIAL: Final = "credencial_ausente"
LIMITER = TokenRateLimiter()
_CNPJ: Final = re.compile(r"[0-9A-Z]{12}[0-9]{2}")
_DAY: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def secret_variable(name: str) -> str:
    """Name the environment variable that holds the deployment secret `name`."""
    return "FOCUS_SECRET_" + re.sub(r"[^A-Z0-9]+", "_", name.upper())


def token_for(connection: ProviderConnection) -> str | None:
    """Return the connection's Focus token for this one send, or None if absent."""
    if connection.environment == "fake":
        return FAKE_TOKEN
    if not connection.secret_ref:
        return None
    return os.environ.get(secret_variable(connection.secret_ref)) or None


def limiter_key(connection: ProviderConnection) -> str:
    """Rate-limit by token identity (its name), never by the token value."""
    return f"{connection.environment}:{connection.secret_ref or 'sem-segredo'}"


def company_request(subject: str) -> ProviderRequest:
    """`GET /v2/empresas/{id}`: the company status read (A6 FC-EMP)."""
    return ProviderRequest(variant="read", subject=subject)


def update_request(
    subject: str, fields: dict[str, bool | str], *, dry_run: bool = False
) -> ProviderRequest:
    """`PUT /v2/empresas/{id}` with exactly `fields`, refused outside the set."""
    if not fields or set(fields) - SETUP_FIELDS:
        raise OperacaoNaoPermitida
    body = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    return ProviderRequest(
        variant="update",
        subject=subject,
        query=(("dry_run", "1"),) if dry_run else (),
        body=body,
    )


def request_digest(request: ProviderRequest) -> str:
    """Digest a request's canonical identity: route variant, subject, query, body."""
    canonical = json.dumps(
        {
            "variant": request.variant,
            "subject": request.subject,
            "query": [list(item) for item in request.query],
            "body": request.body.decode(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CompanyReport:
    """What Focus reports about one company: status, never key material."""

    company_cnpj: str
    reported_cnpj: str
    valid_from: dt.date | None
    valid_to: dt.date | None
    flags: dict[str, bool | str | None] = field(default_factory=dict)


def _normalized_cnpj(value: JSONValue) -> str:
    """Digits/letters only, upper case; anything unrecognisable is blank."""
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"[.\-/\s]", "", value).upper()
    return cleaned if _CNPJ.fullmatch(cleaned) else ""


def _reported_day(value: JSONValue) -> dt.date | None:
    """Focus sends ISO-8601 date-times with offset; keep the local calendar day."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise TypeError
    if _DAY.fullmatch(value):
        return dt.date.fromisoformat(value)
    moment = dt.datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError
    return timezone.localtime(moment).date()


def _flag(value: JSONValue) -> bool | str | None:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, str) and _DAY.fullmatch(value[:10]):
        return value[:10]
    raise TypeError


def parse_company(response: ProviderResponse, subject: str) -> CompanyReport:
    """Accept only a well-formed reply for this exact Focus company id."""
    try:
        body = response.json()
    except TransportFailure:
        raise ValueError from None
    if not isinstance(body, dict) or str(body.get("id")) != subject:
        raise ValueError
    try:
        valid_from = _reported_day(body.get("certificado_valido_de"))
        valid_to = _reported_day(body.get("certificado_valido_ate"))
        flags = {name: _flag(body[name]) for name in SNAPSHOT_FIELDS if name in body}
    except TypeError:
        raise ValueError from None
    return CompanyReport(
        _normalized_cnpj(body.get("cnpj")),
        _normalized_cnpj(body.get("certificado_cnpj")),
        valid_from,
        valid_to,
        flags,
    )


def classify(
    method: Literal["GET", "POST", "PUT"],
    operation_class: str,
    response: ProviderResponse,
) -> CallResult:
    """Outcome from the complete status; only a write that may have landed is unsure.

    Focus calls are included in the plan, so billing is `nao` (todo 10 settles it).
    A 429 or 5xx on a read is retried by the protocol (capture reads) or failed
    (company reads have no effect to be unsure about); on a PUT, a 429 means Focus
    refused before applying anything, while a 5xx may have applied it.
    """
    status = response.status
    code = f"focus_http_{status}"
    digest = hashlib.sha256(response.body).hexdigest()
    retryable = status == 429 or status >= 500  # noqa: PLR2004 - documented limit
    if status == 200:  # noqa: PLR2004 - Focus success
        state = CallState.SUCCESS
    elif not retryable:
        state = CallState.FAILED
    elif method == "GET":
        state = CallState.RETRY if operation_class == "read" else CallState.FAILED
    elif status == 429:  # noqa: PLR2004 - refused before processing
        state = CallState.FAILED
    else:
        state = CallState.UNCERTAIN
    return CallResult(state, status, code, "nao", response_digest=digest)


class FocusAdapter:
    """Send one already-authorized Focus request and keep its reply transiently.

    `report` is set only when a company read succeeded and parsed; the protocol's
    `keep` hook stores it inside the transaction that finalizes the success.
    """

    def __init__(
        self,
        transport: ProviderTransport,
        request: ProviderRequest,
        *,
        token: str | None,
        limiter_key: str,
    ) -> None:
        """Bind one request to a provider-only transport and its token's window."""
        self.transport = transport
        self.request = request
        self._token = token
        self.limiter_key = limiter_key
        self.response: ProviderResponse | None = None
        self.report: CompanyReport | None = None

    def matches(self, digest: str) -> bool:
        """Whether this adapter would send exactly the journaled request."""
        return request_digest(self.request) == digest

    def _unsent(self, dispatch: Dispatch) -> CallResult | None:
        """Refuse a different request, a missing token or a full window unsent."""
        receipt = dispatch.receipt
        if not self.matches(receipt.request_digest):
            return CallResult(CallState.FAILED, None, "pedido_divergente", "nao")
        if self._token is None:
            return CallResult(CallState.FAILED, None, MISSING_CREDENTIAL, "nao")
        if not LIMITER.acquire(self.limiter_key):
            # Nothing was sent: a capture read waits for its next attempt, while a
            # configuration call fails definitively (it never becomes uncertain).
            configuration = receipt.operation_class != "read"
            state = CallState.FAILED if configuration else CallState.RETRY
            return CallResult(state, None, RATE_LIMITED, "nao")
        return None

    def send(self, dispatch: Dispatch) -> CallResult:
        """Send at most once; parse a company read before it can be final."""
        self.response = None
        self.report = None
        refused = self._unsent(dispatch)
        if refused is not None:
            return refused
        receipt = dispatch.receipt
        basic = base64.b64encode(f"{self._token}:".encode()).decode()
        request = replace(
            self.request,
            headers=(
                ("Authorization", f"Basic {basic}"),
                ("Accept", "application/json"),
            ),
        )
        try:
            self.response = self.transport.send(receipt.operation, request)
        except TransportFailure as error:
            return error.result()
        except OperacaoNaoPermitida as error:
            # Raised by the allowlist or the wire permit before the first byte.
            return CallResult(CallState.FAILED, None, error.code, "nao")
        route = ROUTES[(receipt.operation, self.request.variant)]
        result = classify(route.method, receipt.operation_class, self.response)
        if result.state == CallState.SUCCESS and self.request.variant == "read":
            try:
                self.report = parse_company(self.response, self.request.subject)
            except ValueError:
                return replace(
                    result, state=CallState.FAILED, provider_code="resposta_invalida"
                )
        return result
