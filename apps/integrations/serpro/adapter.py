"""One wire send per dispatch; reauthentication belongs to the phased protocol."""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Final, Literal

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied

from apps.core.tenancy import tenant_context
from apps.integrations.allowlist import SERPRO_SERVICES
from apps.integrations.call_contracts import (
    AdmissionDeniedError,
    CallResult,
    IntentConflictError,
    StaleClaimError,
)
from apps.integrations.call_dispatch import Dispatch
from apps.integrations.call_models import CallState
from apps.integrations.models import ClientProviderLink, ProviderConnection
from apps.integrations.serpro.codec import (
    CnpjInvalidoError,
    Parties,
    build_request,
    parse_response,
)
from apps.integrations.serpro.termo import (
    TermoInvalidoError,
    read_signed_termo,
    stored_termo,
)
from apps.integrations.serpro.termo_response import TermoToken, parse_termo_response
from apps.integrations.serpro.termo_state import termo_head
from apps.integrations.serpro.work import PROC_MISSING, own_firm_condition
from apps.integrations.services import IntegrationConflictError
from apps.integrations.token_broker import TokenBrokerClient
from apps.integrations.transport_types import (
    JSONValue,
    OperacaoNaoPermitida,
    ProviderRequest,
    ProviderResponse,
    TransportFailure,
)

if TYPE_CHECKING:
    from uuid import UUID

    from apps.integrations.firm_calls import FirmDispatch
    from apps.integrations.token_broker import BrokerTokens
    from apps.integrations.transport import ProviderTransport

__all__ = [
    "CnpjInvalidoError",
    "Parties",
    "SerproAdapter",
    "build_request",
    "classify",
    "parse_response",
    "parse_termo_response",
]

FREE: Final = frozenset({"SP-TERMO", "SP-SIT-P"})
PROC_DENIED: Final = "AcessoNegado-ICGERENCIADOR-016"
COMPLETE_401: Final = "serpro_401"
TERMO_EXPIRED: Final = "termo_expirado"
# Every way the nested termo re-submission can refuse before or during its send.
RECOVERY_REFUSALS: Final = (
    TransportFailure,
    AdmissionDeniedError,
    IntentConflictError,
    StaleClaimError,
    IntegrationConflictError,
    PermissionDenied,
    ObjectDoesNotExist,
    ValueError,
)


def _codes(response: ProviderResponse) -> tuple[str, ...]:
    """Return denial codes; a malformed body cannot change a complete 403."""
    if not response.body:
        return ()
    try:
        return parse_response(response).codes
    except TransportFailure:
        return ()


def classify(operation: str, response: ProviderResponse) -> CallResult:
    """A6 billing and certainty depend on the complete HTTP response, not prose."""
    if operation not in SERPRO_SERVICES:
        raise OperacaoNaoPermitida
    status = response.status
    billed: Literal["sim", "nao"] = (
        "sim" if operation not in FREE and status in {200, 202, 403} else "nao"
    )
    code = f"serpro_http_{status}"
    if status == 401:  # noqa: PLR2004 - sole definitive authentication refusal
        state, code = CallState.FAILED, COMPLETE_401
    elif status >= 500:  # noqa: PLR2004 - effects may have reached the upstream
        state = CallState.UNCERTAIN
    elif status == 403:  # noqa: PLR2004 - denied calls remain billable
        state = CallState.DENIED
        if PROC_DENIED in _codes(response):
            code = PROC_DENIED
    elif status == 202:  # noqa: PLR2004 - pending provider result
        state = CallState.WAITING
    elif status in {200, 204, 304}:
        state = CallState.SUCCESS
    else:
        state = CallState.FAILED
    return CallResult(
        state,
        status,
        code,
        billed,
        response_digest=hashlib.sha256(response.body).hexdigest(),
    )


class SerproAdapter:
    """Accumulate only this call's transient response for its fenced domain consumer.

    The response slot is deliberately mutable and replaced on each attempt. It is
    never a cache: services discard the adapter when this one call returns.
    """

    def __init__(
        self, transport: ProviderTransport, parties: Parties, data: JSONValue
    ) -> None:
        """Bind fixed request data to a provider-only transport."""
        self.transport = transport
        self.parties = parties
        self.data = data
        self.response: ProviderResponse | None = None
        # Set only for a termo success whose token is usable; see `_termo_outcome`.
        self.termo: TermoToken | None = None

    def _request(self, dispatch: Dispatch | FirmDispatch) -> ProviderRequest:
        operation = dispatch.receipt.operation
        request = build_request(operation, self.parties, self.data)
        if hashlib.sha256(request.body).hexdigest() != dispatch.receipt.request_digest:
            code = "pedido_divergente"
            raise TransportFailure(code)
        connection_id = None
        if isinstance(dispatch, Dispatch):
            with tenant_context(dispatch.claim.tenant_id):
                link = ClientProviderLink.objects.select_related("client").get(
                    pk=dispatch.receipt.link_id, client_id=dispatch.claim.client_id
                )
                if link.client.cnpj != self.parties.client:
                    code = "contribuinte_divergente"
                    raise TransportFailure(code)
                connection_id = link.connection_id
        return replace(request, headers=self._headers(dispatch, connection_id))

    def _bind_firm(self, tenant_id: UUID, connection_id: UUID) -> None:
        """Key broker tokens only by the firm this tenant's own signed termo names.

        The caller's `Parties` cannot choose another firm's broker partition: the
        stored termo governing this connection must name the same firm (INV-R1).
        """
        with tenant_context(tenant_id):
            head = termo_head(connection_id)
            document = None if head is None else head.signed_document
        code = "escritorio_divergente"
        if document is None:
            raise TransportFailure(code)
        try:
            read_signed_termo(stored_termo(document), self.parties)
        except TermoInvalidoError:
            raise TransportFailure(code) from None

    def _production_tokens(
        self, dispatch: Dispatch | FirmDispatch, connection_id: UUID | None
    ) -> BrokerTokens:
        if connection_id is not None:
            self._bind_firm(dispatch.claim.tenant_id, connection_id)
        client = TokenBrokerClient(os.environ.get("SERPRO_BROKER_SECRET", ""))
        tokens = client.tokens(
            self.parties.firm, refresh=dispatch.receipt.attempt_number > 1
        )
        if dispatch.receipt.operation == "SP-TERMO" or tokens.procurador_token:
            return tokens
        from apps.integrations.serpro.authorization import (  # noqa: PLC0415
            recover_procurador,
        )

        if connection_id is None:
            code = "vinculo_ausente"
            raise TransportFailure(code)
        # A restarted broker lost the procurador token: re-submit the stored signed
        # termo (free /Apoiar; a 304 returns the still-valid token). Any refusal is
        # owned firm work, and this call stops before its own first byte.
        try:
            recover_procurador(
                dispatch.claim.tenant_id,
                connection_id,
                Parties(self.parties.contratante, self.parties.firm, self.parties.firm),
            )
        except RECOVERY_REFUSALS:
            with tenant_context(dispatch.claim.tenant_id):
                own_firm_condition(
                    ProviderConnection.objects.get(pk=connection_id),
                    PROC_MISSING,
                    [str(dispatch.claim.call_id)],
                )
            code = PROC_MISSING
            raise TransportFailure(code) from None
        tokens = client.tokens(self.parties.firm)
        if not tokens.procurador_token:
            code = PROC_MISSING
            raise TransportFailure(code)
        return tokens

    def _headers(
        self, dispatch: Dispatch | FirmDispatch, connection_id: UUID | None
    ) -> tuple[tuple[str, str], ...]:
        headers = [("X-Request-Tag", dispatch.receipt.attempt_id.hex)]
        if self.transport.environment == "producao":
            tokens = self._production_tokens(dispatch, connection_id)
            headers.extend(
                (
                    ("Authorization", f"Bearer {tokens.access_token}"),
                    ("jwt_token", tokens.jwt_token),
                )
            )
            if dispatch.receipt.operation != "SP-TERMO":
                headers.append(("autenticar_procurador_token", tokens.procurador_token))
        elif self.transport.environment == "trial":
            bearer = os.environ.get("SERPRO_TRIAL_BEARER", "")
            if not bearer:
                code = "credencial_trial_ausente"
                raise TransportFailure(code)
            headers.append(("Authorization", f"Bearer {bearer}"))
        else:
            headers.append(("Authorization", "Bearer synthetic-serpro"))
        return tuple(headers)

    def send(self, dispatch: Dispatch | FirmDispatch) -> CallResult:
        """Get per-attempt credentials, then consume exactly one issued wire permit."""
        self.response = None
        self.termo = None
        try:
            self.response = self.transport.send(
                dispatch.receipt.operation, self._request(dispatch)
            )
            result = classify(dispatch.receipt.operation, self.response)
        except TransportFailure as error:
            return error.result()
        if (
            dispatch.receipt.operation == "SP-TERMO"
            and result.state == CallState.SUCCESS
        ):
            return self._termo_outcome(result, self.response)
        return result

    def _termo_outcome(
        self, result: CallResult, response: ProviderResponse
    ) -> CallResult:
        """Decide a termo success before it is final: usable, expired or unresolved.

        Nothing after finalization can then fail on the reply: an unusable token or
        expiry is an owned `incerta` registration, never accepted or resent, and an
        already expired token is a failed authorization, never an accepted one.
        """
        try:
            token = parse_termo_response(response)
        except TransportFailure:
            code = "resposta_termo_invalida"
            return replace(result, state=CallState.UNCERTAIN, provider_code=code)
        if token.expires_at <= time.time():
            return replace(result, state=CallState.FAILED, provider_code=TERMO_EXPIRED)
        self.termo = token
        return result
