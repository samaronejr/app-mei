"""Closed, minimized provider receipts independent of the database backup."""

import json
import re
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from typing import Literal
from uuid import UUID

from apps.cycles.effects.manifest import JSONValue, canonicalize_json
from apps.integrations.call_contracts import CallRequest, CallResult, Claim
from apps.integrations.call_models import FirmProviderCall, ProviderCall


class JournalError(Exception):
    """Independent storage is incomplete, malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class JournalRecord:
    """Schema v1 contains no request/response bodies, credentials or arbitrary text."""

    tenant_id: UUID
    client_id: UUID
    link_id: UUID
    call_id: UUID
    attempt_id: UUID
    attempt_number: int
    fence: int
    execution_epoch: int
    operation: str
    operation_revision: str
    operation_class: str
    provider: str
    environment: str
    request_key: str
    request_digest: str
    business_effect_key: str
    billing_class: str
    reserved_microbrl: int
    tariff_revision: str
    occurred_at: datetime
    result: CallResult | None = None
    schema_version: Literal[1] = 1

    @classmethod
    def started(cls, call: ProviderCall, ticket: Claim) -> "JournalRecord":
        """Freeze the committed dispatch identity before independent storage I/O."""
        if call.dispatch_started_at is None:
            raise JournalError
        return cls(
            tenant_id=ticket.tenant_id,
            client_id=ticket.client_id,
            link_id=call.link_id,
            call_id=ticket.call_id,
            attempt_id=ticket.owner,
            attempt_number=call.attempts,
            fence=ticket.fence,
            execution_epoch=ticket.execution_epoch,
            operation=call.operation,
            operation_revision=call.operation_revision,
            operation_class=call.operation_class,
            provider=call.provider,
            environment=call.environment,
            request_key=call.request_key,
            request_digest=call.request_digest,
            business_effect_key=call.business_effect_key,
            billing_class=call.billing_class,
            reserved_microbrl=call.reserved_microbrl,
            tariff_revision=call.tariff_revision,
            occurred_at=call.dispatch_started_at,
        )

    @property
    def path(self) -> str:
        """Start and result have distinct, deterministic immutable object keys."""
        suffix = "start" if self.result is None else "result"
        return (
            f"provider-journal/{self.tenant_id}/{self.occurred_at:%Y/%m}/"
            f"{self.call_id}-{self.attempt_id}-{suffix}.json"
        )

    def encode(self) -> bytes:
        """Serialize only this closed dataclass, never an adapter's response mapping."""
        return json.dumps(asdict(self), default=str, sort_keys=True).encode()


FIRM_PREFIX = "provider-journal-firm"


@dataclass(frozen=True, slots=True)
class FirmJournalRecord:
    """SP-TERMO receipt schema v1: firm identities only, no client, link or call.

    `submission_id` is the `FirmProviderCall`; `authorization_id` the signed termo.
    """

    tenant_id: UUID
    authorization_id: UUID
    connection_id: UUID
    submission_id: UUID
    attempt_id: UUID
    attempt_number: int
    fence: int
    execution_epoch: int
    operation: str
    operation_revision: str
    operation_class: str
    provider: str
    environment: str
    request_key: str
    request_digest: str
    occurred_at: datetime
    result: CallResult | None = None
    schema_version: Literal[1] = 1

    @classmethod
    def started(cls, call: FirmProviderCall) -> "FirmJournalRecord":
        """Freeze the committed dispatch identity before independent storage I/O."""
        if call.dispatch_started_at is None or call.x_request_tag is None:
            raise JournalError
        return cls(
            tenant_id=call.tenant_id,
            authorization_id=call.authorization_id,
            connection_id=call.connection_id,
            submission_id=call.pk,
            attempt_id=call.x_request_tag,
            attempt_number=call.attempts,
            fence=call.fence,
            execution_epoch=call.execution_epoch,
            operation=call.operation,
            operation_revision=call.operation_revision,
            operation_class=call.operation_class,
            provider=call.provider,
            environment=call.environment,
            request_key=call.request_key,
            request_digest=call.request_digest,
            occurred_at=call.dispatch_started_at,
        )

    @property
    def path(self) -> str:
        """A separate prefix: client receipts never parse as firm ones, or back."""
        suffix = "start" if self.result is None else "result"
        return (
            f"{FIRM_PREFIX}/{self.tenant_id}/{self.occurred_at:%Y/%m}/"
            f"{self.submission_id}-{self.attempt_id}-{suffix}.json"
        )

    def encode(self) -> bytes:
        """Serialize only this closed dataclass."""
        return json.dumps(asdict(self), default=str, sort_keys=True).encode()


def _result(result_data: JSONValue) -> CallResult | None:
    result = None
    if result_data is not None:
        if not isinstance(result_data, dict) or set(result_data) != {
            f.name for f in fields(CallResult)
        }:
            raise JournalError
        state, code, billed = (
            result_data[k] for k in ("state", "provider_code", "billed")
        )
        digest, status, amount = (
            result_data[k]
            for k in ("response_digest", "http_status", "settled_microbrl")
        )
        if (
            not isinstance(state, str)
            or not isinstance(code, str)
            or billed not in {"sim", "nao", "desconhecido"}
            or not isinstance(digest, str)
            or (status is not None and type(status) is not int)
            or (amount is not None and type(amount) is not int)
        ):
            raise JournalError
        # Narrow the parsed enum without casting untrusted JSON into a trusted type.
        match billed:
            case "sim" | "nao" | "desconhecido":
                result = CallResult(state, status, code, billed, digest, amount)
            case _:
                raise JournalError
    return result


def decode(raw: bytes, tenant_id: UUID) -> JournalRecord:
    """Parse one bounded object, rejecting extra fields and malformed identities."""
    data: JSONValue = json.loads(canonicalize_json(raw))
    if not isinstance(data, dict) or set(data) != {
        f.name for f in fields(JournalRecord)
    }:
        raise JournalError

    def text(name: str) -> str:
        value = data[name]
        if not isinstance(value, str):
            raise JournalError
        return value

    def integer(name: str) -> int:
        value = data[name]
        if type(value) is not int or value < 0:
            raise JournalError
        return value

    record = JournalRecord(
        tenant_id=UUID(text("tenant_id")),
        client_id=UUID(text("client_id")),
        link_id=UUID(text("link_id")),
        call_id=UUID(text("call_id")),
        attempt_id=UUID(text("attempt_id")),
        attempt_number=integer("attempt_number"),
        fence=integer("fence"),
        execution_epoch=integer("execution_epoch"),
        operation=text("operation"),
        operation_revision=text("operation_revision"),
        operation_class=text("operation_class"),
        provider=text("provider"),
        environment=text("environment"),
        request_key=text("request_key"),
        request_digest=text("request_digest"),
        business_effect_key=text("business_effect_key"),
        billing_class=text("billing_class"),
        reserved_microbrl=integer("reserved_microbrl"),
        tariff_revision=text("tariff_revision"),
        occurred_at=datetime.fromisoformat(text("occurred_at")),
        result=_result(data["result"]),
    )
    CallRequest(
        record.operation,
        record.request_key,
        record.request_digest,
        record.business_effect_key,
    )
    if (
        integer("schema_version") != 1
        or record.tenant_id != tenant_id
        or record.occurred_at.utcoffset() is None
        or record.attempt_number < 1
        or record.fence < 1
        or record.execution_epoch < 1
        or record.provider not in {"serpro", "focus"}
        or record.environment not in {"fake", "trial", "homologacao", "producao"}
        or record.operation_class
        not in {"read", "issuance", "registration", "configuration"}
        or record.billing_class
        not in {"consulta", "emissao", "incluido_no_plano", "nao_faturada"}
        or any(
            re.fullmatch(r"[A-Za-z0-9_.() -]{1,64}", value) is None
            for value in (
                record.operation,
                record.operation_revision,
                record.tariff_revision,
            )
        )
    ):
        raise JournalError
    return record


def decode_firm(raw: bytes, tenant_id: UUID) -> FirmJournalRecord:
    """Parse one bounded firm receipt; any client/link/call key fails closed."""
    data: JSONValue = json.loads(canonicalize_json(raw))
    if not isinstance(data, dict) or set(data) != {
        f.name for f in fields(FirmJournalRecord)
    }:
        raise JournalError

    def text(name: str) -> str:
        value = data[name]
        if not isinstance(value, str):
            raise JournalError
        return value

    def integer(name: str) -> int:
        value = data[name]
        if type(value) is not int or value < 1:
            raise JournalError
        return value

    record = FirmJournalRecord(
        tenant_id=UUID(text("tenant_id")),
        authorization_id=UUID(text("authorization_id")),
        connection_id=UUID(text("connection_id")),
        submission_id=UUID(text("submission_id")),
        attempt_id=UUID(text("attempt_id")),
        attempt_number=integer("attempt_number"),
        fence=integer("fence"),
        execution_epoch=integer("execution_epoch"),
        operation=text("operation"),
        operation_revision=text("operation_revision"),
        operation_class=text("operation_class"),
        provider=text("provider"),
        environment=text("environment"),
        request_key=text("request_key"),
        request_digest=text("request_digest"),
        occurred_at=datetime.fromisoformat(text("occurred_at")),
        result=_result(data["result"]),
    )
    if (
        integer("schema_version") != 1
        or record.tenant_id != tenant_id
        or record.occurred_at.utcoffset() is None
        or record.operation != "SP-TERMO"
        or record.operation_class != "registration"
        or record.provider != "serpro"
        or record.environment not in {"fake", "trial", "homologacao", "producao"}
        or re.fullmatch("[a-f0-9]{64}", record.request_key) is None
        or re.fullmatch("[a-f0-9]{64}", record.request_digest) is None
        or re.fullmatch(r"[A-Za-z0-9_.() -]{1,64}", record.operation_revision) is None
    ):
        raise JournalError
    return record
