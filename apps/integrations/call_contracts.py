"""Typed contracts for the phased provider protocol and its admission hooks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Protocol

from apps.integrations.models import ISSUE_SERVICE_LENGTH

if TYPE_CHECKING:
    from uuid import UUID

    from apps.integrations.call_models import ProviderCall

HTTP_STATUS_MIN: Final = 100
HTTP_STATUS_MAX: Final = 599
MAX_ATTEMPTS: Final = 3


class IntentConflictError(Exception):
    """A deterministic identity was reused with different request bytes."""


class StaleClaimError(Exception):
    """The lease, dispatch fence or execution epoch is no longer current."""


class AdmissionDeniedError(Exception):
    """Live provider, tenant, authority, approval or budget controls refuse I/O."""


@dataclass(frozen=True, slots=True)
class CallRequest:
    """Only canonical identities and digests cross into persisted workflow state."""

    operation: str
    request_key: str
    request_digest: str
    business_effect_key: str

    def __post_init__(self) -> None:
        """Reject malformed identities before any reservation or journal write."""
        for value in (
            self.request_key,
            self.request_digest,
            self.business_effect_key,
        ):
            if re.fullmatch("[a-f0-9]{64}", value) is None:
                message = "Provider identities must be lowercase SHA-256 digests."
                raise ValueError(message)


@dataclass(frozen=True, slots=True)
class CallResult:
    """An adapter receipt contains no response bytes, secrets or arbitrary text."""

    state: str
    http_status: int | None
    provider_code: str
    billed: Literal["sim", "nao", "desconhecido"]
    response_digest: str = ""
    settled_microbrl: int | None = None

    def __post_init__(self) -> None:
        """Accept only execution outcomes and minimized symbolic adapter codes."""
        if (
            self.state
            not in {
                "sucesso",
                "aguardando_resultado",
                "aguardar_nova_tentativa",
                "falhou",
                "negada",
                "incerta",
            }
            or re.fullmatch(r"[A-Za-z0-9_.-]{0,64}", self.provider_code) is None
            or self.billed not in {"sim", "nao", "desconhecido"}
            or (
                self.http_status is not None
                and (
                    type(self.http_status) is not int
                    or not HTTP_STATUS_MIN <= self.http_status <= HTTP_STATUS_MAX
                )
            )
            or (
                self.response_digest != ""
                and re.fullmatch("[a-f0-9]{64}", self.response_digest) is None
            )
            or (
                self.settled_microbrl is not None
                and (
                    type(self.settled_microbrl) is not int or self.settled_microbrl < 0
                )
            )
        ):
            message = "Invalid minimized provider result."
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class Reservation:
    """The explicit maximum liability and the tariff revision that priced it."""

    microbrl: int
    tariff_revision: str

    def __post_init__(self) -> None:
        """Refuse absent tariffs and invalid liabilities rather than pricing at zero."""
        if (
            type(self.microbrl) is not int
            or self.microbrl < 0
            or re.fullmatch(r"[A-Za-z0-9_.() -]{1,64}", self.tariff_revision) is None
        ):
            reason = "bloqueada_cota"
            raise AdmissionDeniedError(reason)


class AdmissionPolicy(Protocol):
    """Authority/approval and budget hooks run under the protocol's DB locks."""

    def authorize(self, call: ProviderCall) -> bool:
        """Recheck authority, approval and reservation against live domain state."""
        ...

    def reserve(self, call: ProviderCall) -> Reservation:
        """Reserve maximum exposure, or raise AdmissionDeniedError before dispatch."""
        ...

    def settle(self, call: ProviderCall, result: CallResult) -> None:
        """Settle x_request_tag once, or resolve its prior uncertain exposure."""
        ...


@dataclass(frozen=True, slots=True)
class Operation:
    """An adapter registers a closed operation with mandatory admission hooks."""

    code: str
    provider: str
    revision: str
    operation_class: Literal["read", "issuance", "registration", "configuration"]
    feature: Literal["federal_checks", "document_capture"]
    billing_class: Literal["consulta", "emissao", "incluido_no_plano", "nao_faturada"]
    policy: AdmissionPolicy

    @property
    def journaled(self) -> bool:
        """Only explicitly unbilled reads skip reservations and the journal."""
        return self.provider == "focus" or not (
            self.operation_class == "read"
            and self.billing_class in {"nao_faturada", "incluido_no_plano"}
        )


@dataclass(frozen=True, slots=True)
class Claim:
    """A committed lease, not permission to skip the dispatch-start recheck."""

    tenant_id: UUID
    client_id: UUID
    call_id: UUID
    fence: int
    owner: UUID
    execution_epoch: int


# Startup registration is deliberately empty: the foundation cannot issue a call
# until a real adapter supplies its authority and budget implementation.
_OPERATIONS: dict[str, Operation] = {}
EXCLUDED_CALL_ROWS: Final = frozenset({"SP-AUTH", "SP-TERMO"})


def register_operation(operation: Operation) -> None:
    """Register a client operation; auth and the firm-level termo have no call row."""
    if (
        operation.code in EXCLUDED_CALL_ROWS
        or operation.code in _OPERATIONS
        or len(operation.code) > ISSUE_SERVICE_LENGTH
    ):
        message = "Operation cannot be registered as a client provider call."
        raise ValueError(message)
    _OPERATIONS[operation.code] = operation


def operation_for(code: str) -> Operation:
    """Unregistered operations fail closed, without a permissive default policy."""
    try:
        return _OPERATIONS[code]
    except KeyError:
        message = "Provider operation has no registered admission policy."
        raise AdmissionDeniedError(message) from None
