"""Obtain the Receita fiscal-situation report (SITFIS) without premature paid polls.

AXRM-016 (plan todo 16; A6 SP-SIT-P/SP-SIT-R, A7.3, A7.4).

* `request_situation_report(client)` opens one report (and its consultation
  receipt) per client at a time; a repeated request returns the active one.
* `advance_situation_report(tenant, report)` is the whole state machine, driven
  only by durable rows so any runner, redelivery or restart continues it:
  `solicitado -> protocolo_recebido -> aguardando -> emitindo -> emitido`, with the
  alternatives `negado | falha | incerto | protocolo_expirado |
  limite_de_tentativas`. Every SERPRO call goes through the AXRM-009 protocol
  (reserve, claim, journal, fenced finalize) with the AXRM-010 budget.
* SOLICITARPROTOCOLO91 (`/Apoiar`, free) returns `protocoloRelatorio` and
  `tempoEspera` in milliseconds; RELATORIOSITFIS92 (`/Emitir`, billed) is never
  called before `wait_until`. A 202 (billed) carries a new `tempoEspera` in
  `dados`; a 204 (not billed) carries it in the `ETag` header. Each emission
  attempt is its own call and identity; at most three, then
  `limite_de_tentativas`.
* Each call retains its own received-at timestamp and wait, independently of
  finalization, quarantine and report progress. The fenced admission hook checks
  the maximum announced deadline at reserve, claim and dispatch start.
* A paid PDF and its report association commit together before finalization.
  Every report state can adopt it, including `limite_de_tentativas`. A failure
  before that private commit remains the disclosed pre-durable window.

The PDF is never parsed: it is stored as an actor-less, portal-invisible
`provedor_federal` original through `ingest_system_document` and must pass the
ordinary scan before anyone reviews it. No protocol, report byte or provider
text reaches a log, an exception or an event.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, cast

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.services import ObjectRef, record_event
from apps.authz.objects import load_visible_client
from apps.authz.services import can
from apps.clients.models import ClientCompany
from apps.core.flags import tenant_has
from apps.core.retention import RetentionClass
from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.cycles.intake import UploadRejected, UploadScannerOff
from apps.cycles.models import CycleEvent, ExceptionState, ServiceCycle
from apps.cycles.models import Exception as CycleException
from apps.cycles.openers import CLOSED_STATES
from apps.cycles.scanning import scan_required
from apps.cycles.services.exceptions import ACTIVE_STATES, ExceptionReason
from apps.integrations.authority import (
    Problem,
    authority_for,
    raise_problem,
    record_authority_denial,
)
from apps.integrations.budget_pricing import BudgetDeniedError
from apps.integrations.budget_settlement import release
from apps.integrations.call_contracts import (
    AdmissionDeniedError,
    CallRequest,
    CallResult,
    Claim,
    IntentConflictError,
    StaleClaimError,
)
from apps.integrations.call_dispatch import abort_unopened
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_protocol import claim, perform, reserve
from apps.integrations.call_state import current_epoch
from apps.integrations.models import (
    ACTIVE_ISSUE_STATES,
    ClientProviderLink,
    IntegrationIssue,
    IssueReason,
    IssueState,
    LinkStatus,
)
from apps.integrations.serpro.adapter import SerproAdapter
from apps.integrations.serpro.codec import build_request, parse_response
from apps.integrations.serpro.parties import termo_parties
from apps.integrations.serpro.sitfis_wire import ReportWireBusyError, report_mutex
from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import TransportFailure
from apps.integrations.transport_wire import XML_LIMIT
from apps.obligations.models import (
    AcquisitionOrigin,
    Document,
    FederalConsultation,
    FederalConsultationKind,
    FederalConsultationOutcome,
    FiscalReportState,
    FiscalSituationReport,
)
from apps.obligations.models.federal import MAX_EMISSION_ATTEMPTS
from apps.obligations.system_ingest import ingest_system_document as _ingest_document

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from uuid import UUID

    from apps.accounts.models import User
    from apps.integrations.call_dispatch import Dispatch
    from apps.integrations.serpro.codec import Parties
    from apps.integrations.transport_types import (
        Environment,
        JSONValue,
        ProviderResponse,
    )

logger = logging.getLogger(__name__)

PROTOCOL_OPERATION: Final = "SP-SIT-P"
EMISSION_OPERATION: Final = "SP-SIT-R"
CHECKS_RUN: Final = "fiscal.checks.run"
DEFAULT_WAIT: Final = timedelta(seconds=60)
# A provider estimate is honoured up to one day; longer ones are clamped, never
# shortened to the default (a shorter wait would be a premature, billed poll).
MAX_WAIT_MS: Final = 86_400_000
# The SP-SIT-R reply is JSON under AXRM-011's 5 MiB wire cap and carries the PDF as
# base64 (4/3 larger) inside an escaped envelope: this is the largest report the
# wire can deliver, and the one PDF limit the adapter enforces.
ENVELOPE_MARGIN: Final = 4096
MAX_REPORT_BYTES: Final = (XML_LIMIT - ENVELOPE_MARGIN) // 4 * 3
NOT_STORED: Final = "relatorio_nao_armazenado"
# Rescheduling a delivery that found another runner's live lease.
LEASE_MARGIN: Final = timedelta(seconds=30)
INVALID_REPLY: Final = "resposta_sitfis_invalida"
REPORT_FAILED: Final = IssueReason.SITUACAO_FISCAL_NAO_OBTIDA.value
# One protocol plus three emissions, with slack for one quarantined lease.
MAX_STEPS: Final = 6
MAX_PROTOCOL_LENGTH: Final = 255
_ETAG_WAIT = re.compile(r'^(?:W/)?"?tempoEspera:(\d{1,10})"?$')

FINAL_STATES: Final = frozenset(
    {
        FiscalReportState.EMITIDO,
        FiscalReportState.NEGADO,
        FiscalReportState.FALHA,
        FiscalReportState.INCERTO,
        FiscalReportState.PROTOCOLO_EXPIRADO,
        FiscalReportState.LIMITE_DE_TENTATIVAS,
    }
)
_OUTCOME: Final[dict[str, str]] = {
    FiscalReportState.EMITIDO: FederalConsultationOutcome.ENCONTRADO,
    FiscalReportState.NEGADO: FederalConsultationOutcome.SEM_PROCURACAO,
    FiscalReportState.FALHA: FederalConsultationOutcome.FALHA,
    FiscalReportState.INCERTO: FederalConsultationOutcome.INCERTO,
    FiscalReportState.PROTOCOLO_EXPIRADO: FederalConsultationOutcome.FALHA,
    FiscalReportState.LIMITE_DE_TENTATIVAS: FederalConsultationOutcome.FALHA,
}
REASONS: Final = {
    REPORT_FAILED: ExceptionReason(
        "Confira o relatório de situação fiscal na Receita: ele não foi obtido "
        "(recusa, falha, resultado incerto, protocolo expirado ou limite de "
        "tentativas). Nenhuma nova emissão é feita automaticamente; peça um novo "
        "relatório quando a causa estiver resolvida.",
        label="relatório de situação fiscal não obtido",
    ),
}


class SituationReportRefusedError(ValidationError):
    """A report cannot be requested now; nothing was written or sent."""

    def __init__(self, code: str) -> None:
        """Carry a symbolic refusal code, never provider or taxpayer values."""
        super().__init__("Relatório de situação fiscal indisponível.", code=code)


class EmissionDeferredError(Exception):
    """A domain veto rolls back admission before any dispatch-start or spending."""

    def __init__(self, call: ProviderCall | Claim) -> None:
        """Carry the actual fenced claim, including an internal 401 retry."""
        super().__init__()
        self.call_id = call.call_id if isinstance(call, Claim) else call.pk
        self.fence = call.fence
        self.owner = call.owner if isinstance(call, Claim) else call.lease_owner


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def protocol_request_key(report_id: UUID) -> str:
    """Return the single SP-SIT-P identity of a report: one protocol request."""
    return _digest("sitfis-protocolo", str(report_id))


def emission_request_key(report_id: UUID, attempt: int) -> str:
    """One SP-SIT-R identity per emission attempt (1..3) of a report."""
    return _digest("sitfis-emissao", str(report_id), str(attempt))


def _transport(environment: Environment) -> ProviderTransport:
    if environment == "fake":
        return ProviderTransport(
            "serpro", "fake", getattr(settings, "SERPRO_FAKE_ORIGIN", "")
        )
    return ProviderTransport("serpro", environment)


def serpro_link(client: ClientCompany) -> ClientProviderLink | None:
    """Return the client's active link on this deployment's SERPRO environment."""
    return (
        ClientProviderLink.objects.filter(
            client_id=client.pk,
            connection__provider="serpro",
            connection__environment=settings.SERPRO_ENVIRONMENT,
            status=LinkStatus.ATIVA,
        )
        .select_related("connection")
        .order_by("pk")
        .first()
    )


def _scanner_off(tenant_id: UUID) -> bool:
    """Tell whether the PDF could not be stored, so no report is paid for."""
    return not settings.CLAMAV_ENABLED and scan_required(tenant_id)


def _wait_from(value: object) -> timedelta | None:
    """`tempoEspera` is an integer number of milliseconds; anything else is absent."""
    if type(value) is not int or value < 0:
        return None
    return timedelta(milliseconds=min(value, MAX_WAIT_MS))


@dataclass(slots=True)
class SitfisReply:
    """Hold one call's transient, already-validated reply; never persisted."""

    protocol: str = field(default="", repr=False)
    wait: timedelta | None = None
    pdf: bytes | None = field(default=None, repr=False)
    document_id: UUID | None = None
    received_at: datetime | None = None


class SitfisAdapter:
    """Decide every SITFIS reply, and record it durably, before it is final.

    A malformed success is a failed call (billed by its HTTP status), never a
    success whose processing could raise after finalization. A valid reply is
    written to the report before the protocol finalizes the call: the protocol,
    SERPRO's wait, and the paid PDF stored as a private original. Whoever applies
    the call later (this runner, a redelivery, a runner taking over an expired
    lease) uses that record, so a received reply is never lost or bought again.
    """

    def __init__(self, inner: SerproAdapter, tenant_id: UUID, report_id: UUID) -> None:
        """Wrap the AXRM-013 adapter for one report's call."""
        self.inner = inner
        self.tenant_id = tenant_id
        self.report_id = report_id
        self.reply = SitfisReply()

    def send(self, dispatch: Dispatch) -> CallResult:
        """Send once through the AXRM-013 adapter, then validate and record."""
        self.reply = SitfisReply()
        result = self.inner.send(dispatch)
        response = self.inner.response
        if response is None:
            return result
        self.reply.received_at = timezone.now()
        operation = dispatch.receipt.operation
        call_id = dispatch.receipt.call_id
        if operation == PROTOCOL_OPERATION and result.state == CallState.SUCCESS:
            result = self._protocol(result, response)
            if self.reply.protocol:
                _receive(self.tenant_id, self.report_id, call_id, self.reply)
            return result
        if operation == EMISSION_OPERATION:
            if result.state == CallState.SUCCESS and response.status == 200:  # noqa: PLR2004
                result = self._report(result, response)
                return self._keep_pdf(result, call_id)
            if result.state == CallState.SUCCESS:
                self.reply.wait = _etag_wait(response)
            elif result.state == CallState.WAITING:
                self.reply.wait = _dados_wait(response)
            if result.state in {CallState.SUCCESS, CallState.WAITING}:
                _receive(self.tenant_id, self.report_id, call_id, self.reply)
        return result

    def _protocol(self, result: CallResult, response: ProviderResponse) -> CallResult:
        data = _data(response) if response.status == 200 else None  # noqa: PLR2004
        protocol = data.get("protocoloRelatorio") if data is not None else None
        if (
            not isinstance(protocol, str)
            or not protocol.strip()
            or len(protocol) > MAX_PROTOCOL_LENGTH
            or re.search(r"[\x00-\x1f\x7f]", protocol)
        ):
            return _invalid(result)
        self.reply.protocol = protocol
        self.reply.wait = _wait_from(data.get("tempoEspera")) if data else None
        return result

    def _report(self, result: CallResult, response: ProviderResponse) -> CallResult:
        data = _data(response)
        encoded = data.get("pdf") if data is not None else None
        if not isinstance(encoded, str):
            return _invalid(result)
        try:
            pdf = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return _invalid(result)
        if not pdf.startswith(b"%PDF-") or len(pdf) > MAX_REPORT_BYTES:
            return _invalid(result)
        self.reply.pdf = pdf
        return result

    def _keep_pdf(self, result: CallResult, call_id: UUID) -> CallResult:
        """Store the paid PDF privately now; a storage failure is a billed failure."""
        if self.reply.pdf is None:
            return result
        try:
            # A durable private original always has its report association, even
            # when intake reused existing bytes with another acquisition receipt.
            document = ingest_system_document(
                tenant_id=self.tenant_id,
                report_id=self.report_id,
                call_id=call_id,
                reply=self.reply,
                payload=self.reply.pdf,
            )
            self.reply.document_id = document.pk
        except (
            UploadScannerOff,
            UploadRejected,
            OSError,
            BotoCoreError,
            ClientError,
        ) as error:
            # Billed (by the HTTP 200) and never requested again automatically.
            logger.warning(
                "SITFIS report not stored",
                extra={"sitfis_event": NOT_STORED, "code": type(error).__name__},
            )
            return CallResult(
                CallState.FAILED,
                result.http_status,
                NOT_STORED,
                result.billed,
                result.response_digest,
            )
        _receive(self.tenant_id, self.report_id, call_id, self.reply)
        return result


def _client_of(tenant_id: UUID, report_id: UUID) -> UUID:
    with tenant_context(tenant_id):
        client_id: UUID = FiscalSituationReport.objects.values_list(
            "client_id", flat=True
        ).get(pk=report_id)
    return client_id


def ingest_system_document(
    *,
    tenant_id: UUID,
    report_id: UUID,
    call_id: UUID,
    reply: SitfisReply,
    payload: bytes,
) -> Document:
    """Commit the private original and report association behind the wire mutex."""
    with report_mutex(tenant_id, report_id), tenant_context(tenant_id):
        document = _ingest_document(
            tenant_id=tenant_id,
            client_id=_client_of(tenant_id, report_id),
            origin=AcquisitionOrigin.FEDERAL_PROVIDER,
            receipt_ref=str(call_id),
            payload=payload,
            filename=f"situacao-fiscal-{report_id}.pdf",
            content_type="application/pdf",
            retention_class=RetentionClass.FISCAL_EVIDENCE,
        )
        kept = replace(reply, document_id=document.pk)
        _store_reply(_locked(report_id), call_id, kept)
        return document


def _receive(
    tenant_id: UUID, report_id: UUID, call_id: UUID, reply: SitfisReply
) -> None:
    """Record this call's receipt in every call and report state."""
    with report_mutex(tenant_id, report_id), tenant_context(tenant_id):
        _store_reply(_locked(report_id), call_id, reply)


def _store_reply(
    report: FiscalSituationReport, call_id: UUID, reply: SitfisReply
) -> None:
    """Append an idempotent call-scoped receipt under the report row lock."""
    wait_ms = int(reply.wait.total_seconds() * 1000) if reply.wait is not None else None
    received = reply.received_at or timezone.now()
    key = (
        ProviderCall.objects.filter(pk=call_id, client_id=report.client_id)
        .values_list("request_key", flat=True)
        .first()
    )
    if key not in _call_keys(report):
        return
    receipt = {"received_at": received.isoformat(), "wait_ms": wait_ms}
    receipts = report.call_replies.setdefault(str(call_id), [])
    if receipt not in receipts:
        receipts.append(receipt)
    # Preserve the old current-attempt projection for receipts and diagnostics.
    # It is deliberately separate from the append-only timing ledger above.
    current = key is not None and key == _next(report)[1]
    fields = ["call_replies"]
    if reply.document_id is not None and report.document_id is None:
        report.document_id = reply.document_id
        report.emitted_at = received
        report.reply_call = str(call_id)
        report.reply_wait_ms = None
        fields += ["document", "emitted_at", "reply_call", "reply_wait_ms"]
    elif current and report.document_id is None:
        report.reply_call = str(call_id)
        report.reply_wait_ms = wait_ms
        fields += ["reply_call", "reply_wait_ms"]
        if (
            reply.protocol
            and report.state == FiscalReportState.SOLICITADO
            and not report.protocol
        ):
            report.protocol = reply.protocol
            report.protocol_date = received
            fields += ["protocol", "protocol_date"]
    deadline = _announced_until(report)
    if deadline is not None and (
        report.wait_until is None or deadline > report.wait_until
    ):
        report.wait_until = deadline
        fields.append("wait_until")
    report.save(update_fields=fields)


def _invalid(result: CallResult) -> CallResult:
    return CallResult(
        CallState.FAILED,
        result.http_status,
        INVALID_REPLY,
        result.billed,
        result.response_digest,
    )


def _data(response: ProviderResponse) -> dict[str, JSONValue] | None:
    try:
        data = parse_response(response).data
    except TransportFailure:
        return None
    return data if isinstance(data, dict) else None


def _dados_wait(response: ProviderResponse) -> timedelta | None:
    data = _data(response)
    return _wait_from(data.get("tempoEspera")) if data is not None else None


def _etag_wait(response: ProviderResponse) -> timedelta | None:
    for name, value in response.headers:
        if name.lower() == "etag":
            match = _ETAG_WAIT.match(value.strip())
            return _wait_from(int(match[1])) if match else None
    return None


def _default_wait(report: FiscalSituationReport, operation: str) -> timedelta:
    logger.info(
        "SITFIS wait estimate absent; using the default",
        extra={
            "sitfis_event": "espera_padrao",
            "report": str(report.pk),
            "operation": operation,
        },
    )
    return DEFAULT_WAIT


def _save(report: FiscalSituationReport) -> None:
    report.version += 1
    report.updated_at = timezone.now()
    report.save()


def _own(report: FiscalSituationReport, operation: str) -> None:
    """Open the report problem on every open cycle, else as an integration issue."""
    raise_problem(
        report.client,
        Problem(
            REPORT_FAILED,
            report.pk,
            review_on=timezone.localdate(),
            service=operation,
        ),
    )


def _finish(
    report: FiscalSituationReport,
    state: str,
    operation: str,
    call: ProviderCall | None,
    outcome: str | None = None,
) -> None:
    """End the report, close its consultation receipt and own any failure."""
    report.state = state
    _save(report)
    consultation = FederalConsultation.objects.select_for_update().get(
        pk=report.consultation_id
    )
    consultation.outcome = outcome or _OUTCOME[state]
    consultation.consulted_at = (
        call.finalized_at if call is not None and call.finalized_at else timezone.now()
    )
    consultation.provider_ref = str(call.pk) if call is not None else ""
    consultation.version += 1
    consultation.save()
    if state != FiscalReportState.EMITIDO:
        _own(report, operation)


def _emitted_event(report: FiscalSituationReport) -> None:
    """Record a system event on each open cycle of the client; ids only (A7.8)."""
    cycles = ServiceCycle.objects.filter(client_id=report.client_id).exclude(
        state__in=CLOSED_STATES
    )
    for cycle in cycles.order_by("pk"):
        CycleEvent.objects.create(
            tenant_id=report.tenant_id,
            client_id=report.client_id,
            cycle=cycle,
            actor=None,
            type="federal.report.emitted",
            aggregate_type="FiscalSituationReport",
            aggregate_id=report.pk,
            aggregate_version=report.version,
            payload={
                "report_id": str(report.pk),
                "document_id": str(report.document_id),
            },
        )


def _stored_wait(
    report: FiscalSituationReport, call_id: UUID, operation: str
) -> timedelta:
    """SERPRO's own wait for this call's reply; the default only when it had none."""
    for receipt in reversed(report.call_replies.get(str(call_id), [])):
        if receipt["wait_ms"] is not None:
            return timedelta(milliseconds=receipt["wait_ms"])
    # An upgrade may find a reply recorded before the per-call ledger existed.
    if report.reply_call == str(call_id) and report.reply_wait_ms is not None:
        return timedelta(milliseconds=report.reply_wait_ms)
    return _default_wait(report, operation)


def _call_keys(report: FiscalSituationReport) -> tuple[str, ...]:
    return (
        protocol_request_key(report.pk),
        *(
            emission_request_key(report.pk, n)
            for n in range(1, MAX_EMISSION_ATTEMPTS + 1)
        ),
    )


def _announced_until(report: FiscalSituationReport) -> datetime | None:
    """Fold every sent call's recorded received-at + wait into one deadline."""
    sent = ProviderCall.objects.filter(
        client_id=report.client_id, request_key__in=_call_keys(report), attempts__gt=0
    ).values_list("pk", flat=True)
    deadlines = [
        datetime.fromisoformat(receipt["received_at"])
        + timedelta(milliseconds=receipt["wait_ms"])
        for call_id in sent
        for receipt in report.call_replies.get(str(call_id), [])
        if receipt["wait_ms"] is not None
    ]
    return max(deadlines, default=None)


def _emission_due(report: FiscalSituationReport) -> bool:
    """One timing rule for planning and fenced reserve, claim and dispatch."""
    deadlines = [
        date
        for date in (report.wait_until, _announced_until(report))
        if date is not None
    ]
    return not deadlines or timezone.now() >= max(deadlines)


def _recover_pdf(report: FiscalSituationReport) -> None:
    """Recover private originals committed by an earlier version of the worker."""
    if report.document_id is not None:
        return
    calls = {
        str(call.pk): call
        for call in ProviderCall.objects.filter(
            client_id=report.client_id,
            operation=EMISSION_OPERATION,
            request_key__in=_call_keys(report),
            attempts__gt=0,
        )
    }
    document = (
        Document.objects.filter(
            client_id=report.client_id,
            acquisition_origin=AcquisitionOrigin.FEDERAL_PROVIDER,
            acquisition_receipt_ref__in=calls,
            portal_visible=False,
            purged_at__isnull=True,
        )
        .order_by("created_at", "pk")
        .first()
    )
    if document is not None:
        report.document_id = document.pk
        report.reply_call = document.acquisition_receipt_ref
        report.emitted_at = document.created_at
        report.save(update_fields=["document", "reply_call", "emitted_at"])


def admit_emission(call: ProviderCall) -> None:
    """Veto under the protocol fence before an attempt or effect barrier exists.

    The shared policy calls this at reserve, claim and dispatch start. Raising a
    domain exception rolls that transaction back; the SITFIS runner then restores
    its own unsent claim to RESERVED and projects the durable wait or paid PDF.
    Calls made by qualification code without a SITFIS report keep their contract.
    """
    reports = FiscalSituationReport.objects.select_for_update().filter(
        client_id=call.client_id
    )
    for report in reports.order_by("pk"):
        if call.request_key not in _call_keys(report):
            continue
        _recover_pdf(report)
        if (
            report.document_id is not None
            or report.state in FINAL_STATES
            or not _emission_due(report)
        ):
            raise EmissionDeferredError(call)
        return


def _wire_report_id(dispatch: Dispatch) -> UUID | None:
    with tenant_context(dispatch.claim.tenant_id):
        reports = FiscalSituationReport.objects.filter(
            client_id=dispatch.claim.client_id
        ).order_by("pk")
        for report in reports:
            if dispatch.receipt.request_key in _call_keys(report):
                return report.pk
    return None


@contextmanager
def wire_guard(dispatch: Dispatch) -> Iterator[None]:
    """Keep the last locked send rule atomic with receipt and PDF commits."""
    ticket = dispatch.claim
    report_id = _wire_report_id(dispatch)
    if report_id is None:
        # Qualification calls have no report; their existing contract is retained.
        yield
        return
    try:
        with report_mutex(ticket.tenant_id, report_id):
            with tenant_context(ticket.tenant_id):
                report = _locked(report_id)
                _recover_pdf(report)
                live = ProviderCall.objects.filter(
                    pk=ticket.call_id,
                    client_id=ticket.client_id,
                    state=CallState.STARTED,
                    fence=ticket.fence,
                    lease_owner=ticket.owner,
                    execution_epoch=ticket.execution_epoch,
                    lease_until__gt=timezone.now(),
                ).exists()
                deferred = (
                    not live
                    or current_epoch() != ticket.execution_epoch
                    or report.document_id is not None
                    or report.state in FINAL_STATES
                    or not _emission_due(report)
                )
            if not deferred:
                yield
                return
    except ReportWireBusyError:
        # Acquisition failed before the guard could authorize a paid byte.
        pass
    # Both the report mutex and its eligibility transaction have ended. Keeping
    # the immutable zero-byte closure may perform storage I/O.
    abort_unopened(dispatch)
    raise EmissionDeferredError(ticket)


def _accept_protocol(report: FiscalSituationReport) -> None:
    """Move a report whose protocol was received on to its wait (any runner)."""
    received = report.protocol_date or timezone.now()
    if report.reply_wait_ms is not None:
        wait = timedelta(milliseconds=report.reply_wait_ms)
    else:
        wait = _default_wait(report, PROTOCOL_OPERATION)
    report.wait_until = received + wait
    report.state = FiscalReportState.PROTOCOLO_RECEBIDO
    _save(report)


def _attempt_of(report: FiscalSituationReport, call: ProviderCall) -> int:
    for attempt in range(1, MAX_EMISSION_ATTEMPTS + 1):
        if call.request_key == emission_request_key(report.pk, attempt):
            return attempt
    return report.emission_attempts


def _adopt(report: FiscalSituationReport, call: ProviderCall | None) -> None:
    """End a report whose paid PDF was received and stored: never buy it again."""
    # A decision may find a durable PDF after another runner reserved the next
    # call. An unlocked unsent identity is cancelled and its budget returned.
    # A currently locked admission will see EMITIDO and roll itself back.
    unused = ProviderCall.objects.select_for_update(skip_locked=True).filter(
        client_id=report.client_id,
        request_key__in=_call_keys(report),
        state__in={CallState.RESERVED, CallState.CLAIMED, CallState.RETRY},
        dispatch_started_at__isnull=True,
    )
    for pending in unused.order_by("pk"):
        release(pending)
        pending.state = CallState.CANCELLED
        pending.version += 1
        pending.save()
    if report.state == FiscalReportState.EMITIDO:
        return
    if call is not None:
        report.emission_attempts = max(
            report.emission_attempts, _attempt_of(report, call)
        )
    report.emitted_at = report.emitted_at or timezone.now()
    _finish(report, FiscalReportState.EMITIDO, EMISSION_OPERATION, call)
    _resolve_report_problem(report)
    _emitted_event(report)


def _resolve_report_problem(report: FiscalSituationReport) -> None:
    """Resolve only this report's failure, retaining ownership and its history."""
    problems = CycleException.objects.filter(
        client_id=report.client_id,
        subject_ref=report.pk,
        reason_code=REPORT_FAILED,
        state__in=ACTIVE_STATES,
    ).order_by("cycle_id", "pk")
    for candidate in problems:
        cycle = ServiceCycle.objects.select_for_update().get(pk=candidate.cycle_id)
        problem = CycleException.objects.select_for_update().get(pk=candidate.pk)
        if problem.state not in ACTIVE_STATES:
            continue
        previous = problem.state
        problem.state = ExceptionState.RESOLVED
        problem.version += 1
        problem.save(update_fields=["state", "version"])
        cycle.version += 1
        cycle.save(update_fields=["version"])
        CycleEvent.objects.create(
            tenant_id=report.tenant_id,
            client_id=report.client_id,
            cycle=cycle,
            actor=None,
            type="exception.transitioned",
            aggregate_type="Exception",
            aggregate_id=problem.pk,
            aggregate_version=problem.version,
            payload={
                "from": previous,
                "to": ExceptionState.RESOLVED,
                "reason": "relatorio_pago_adotado",
                "report_id": str(report.pk),
            },
        )
    issues = IntegrationIssue.objects.select_for_update().filter(
        client_id=report.client_id,
        reason=REPORT_FAILED,
        service=EMISSION_OPERATION,
        state__in=ACTIVE_ISSUE_STATES,
        evidence_refs__contains=[str(report.pk)],
    )
    for issue in issues.order_by("pk"):
        # An issue may own several reports. Keep it active while another report
        # still needs work, and retain every evidence reference.
        other_reports = FiscalSituationReport.objects.filter(
            pk__in=[ref for ref in issue.evidence_refs if ref != str(report.pk)]
        ).exclude(state=FiscalReportState.EMITIDO)
        if other_reports.exists():
            continue
        issue.state = IssueState.RESOLVIDA
        issue.version += 1
        issue.updated_at = timezone.now()
        issue.save(update_fields=["state", "version", "updated_at"])
        record_event(
            action="integrations.issue_resolved",
            tenant_id=report.tenant_id,
            obj=ObjectRef("IntegrationIssue", str(issue.pk)),
            metadata={
                "reason": REPORT_FAILED,
                "service": EMISSION_OPERATION,
                "resolution": "relatorio_pago_adotado",
            },
        )


def _keeper(report_id: UUID, step: _Step) -> Callable[[ProviderCall], None]:
    """Apply the recorded reply in the transaction that finalizes the success."""

    def keep(call: ProviderCall) -> None:
        report = FiscalSituationReport.objects.select_for_update().get(pk=report_id)
        if call.request_key != step.key:
            return
        if step.operation == PROTOCOL_OPERATION:
            if report.protocol and report.state == FiscalReportState.SOLICITADO:
                _accept_protocol(report)
        elif report.document_id is not None:
            _adopt(report, call)

    return keep


@dataclass(frozen=True, slots=True)
class _Step:
    """One call this runner may make: never more than the report's next one."""

    operation: str
    key: str
    attempt: int
    data: JSONValue = field(repr=False)
    parties: Parties = field(repr=False)
    link_id: UUID
    environment: str


def _admissible(client: ClientCompany) -> tuple[ClientProviderLink, Parties]:
    """Refuse before any row when the report could not be requested at all."""
    link: ClientProviderLink | None = None
    parties: Parties | None = None
    code = ""
    if not tenant_has(client.tenant_id, "federal_checks"):
        code = "verificacoes_federais_desligadas"
    elif settings.PROVIDERS_PAUSED or settings.SERPRO_PAUSED:
        code = "integracao_pausada"
    elif _scanner_off(client.tenant_id):
        code = "verificador_desligado"
    else:
        link = serpro_link(client)
        parties = termo_parties(link.connection_id, client.cnpj) if link else None
        if link is None:
            code = "vinculo_ausente"
        elif parties is None:
            code = "termo_ausente"
        elif not authority_for(client, PROTOCOL_OPERATION, link=link).usable:
            code = "sem_procuracao"
    if code or link is None or parties is None:
        raise SituationReportRefusedError(code or "vinculo_ausente")
    return link, parties


def request_situation_report(
    client: ClientCompany, *, actor: User | None = None, enqueue: bool = True
) -> FiscalSituationReport:
    """Open (or return the active) report for a client; runs in its tenant context.

    `actor=None` is the system (the monthly schedule); a staff request must see
    the client in its portfolio and hold `fiscal.checks.run`. The provider is
    contacted only by the worker task queued once this request commits (the
    qualification CLI passes `enqueue=False` and advances the report itself).
    """
    if actor is not None:
        client = load_visible_client(actor, client.pk)
        if not can(actor, CHECKS_RUN, tenant_id=client.tenant_id):
            raise PermissionDenied
    link, parties = _admissible(client)
    digest = hashlib.sha256(
        build_request(PROTOCOL_OPERATION, parties, "").body
    ).hexdigest()
    with transaction.atomic():
        locked = ClientCompany.objects.select_for_update(no_key=True).get(pk=client.pk)
        report = (
            FiscalSituationReport.objects.filter(client_id=locked.pk)
            .exclude(state__in=FINAL_STATES)
            .order_by("created_at", "pk")
            .first()
        )
        if report is None:
            now = timezone.now()
            consultation = FederalConsultation.objects.create(
                tenant_id=locked.tenant_id,
                client=locked,
                kind=FederalConsultationKind.SITUACAO_FISCAL,
                scope_digest=digest,
                consulted_at=now,
                # Until the report ends; then its real outcome replaces this.
                outcome=FederalConsultationOutcome.INCERTO,
                environment=link.connection.environment,
            )
            report = FiscalSituationReport.objects.create(
                tenant_id=locked.tenant_id,
                client=locked,
                consultation=consultation,
                requested_at=now,
                state=FiscalReportState.SOLICITADO,
            )
            record_event(
                action="integrations.situation_report_requested",
                tenant_id=locked.tenant_id,
                actor=actor,
                obj=ObjectRef("FiscalSituationReport", str(report.pk)),
                metadata={},
            )
        args = (str(report.tenant_id), str(report.pk))

        def queue() -> None:
            from apps.integrations.serpro.sitfis_tasks import (  # noqa: PLC0415
                advance_situation_report_task,
            )

            advance_situation_report_task.delay(*args)

        if enqueue:
            transaction.on_commit(queue)
    return report


def _locked(report_id: UUID) -> FiscalSituationReport:
    return (
        FiscalSituationReport.objects.select_for_update(of=("self",))
        .select_related("client")
        .get(pk=report_id)
    )


def _next(report: FiscalSituationReport) -> tuple[str, str, int]:
    if report.state == FiscalReportState.SOLICITADO:
        return PROTOCOL_OPERATION, protocol_request_key(report.pk), 0
    attempt = report.emission_attempts + 1
    return EMISSION_OPERATION, emission_request_key(report.pk, attempt), attempt


def _step(
    report: FiscalSituationReport, operation: str, key: str, attempt: int
) -> _Step | None:
    """Build the next call, or end the report when it can no longer be made."""
    link = serpro_link(report.client)
    parties = termo_parties(link.connection_id, report.client.cnpj) if link else None
    if link is None or parties is None:
        state = FiscalReportState.NEGADO if link else FiscalReportState.FALHA
        _finish(report, state, operation, None)
        return None
    if operation == EMISSION_OPERATION and _scanner_off(report.tenant_id):
        _finish(report, FiscalReportState.FALHA, operation, None)
        return None
    data: JSONValue = (
        ""
        if operation == PROTOCOL_OPERATION
        else {"protocoloRelatorio": report.protocol}
    )
    return _Step(
        operation, key, attempt, data, parties, link.pk, link.connection.environment
    )


def _received_pdf(report: FiscalSituationReport) -> _Step | None:
    """Adopt a durable paid PDF in every state, without another emission."""
    paid = (
        ProviderCall.objects.filter(
            client_id=report.client_id, pk=report.reply_call
        ).first()
        if report.reply_call
        else None
    )
    _adopt(report, paid)
    return None


def _first_call(
    report: FiscalSituationReport, operation: str, key: str, attempt: int
) -> _Step | None:
    """Name a call not made yet; an emission only at or after `wait_until`."""
    if operation == EMISSION_OPERATION:
        if not _emission_due(report):
            if report.state != FiscalReportState.AGUARDANDO:
                report.state = FiscalReportState.AGUARDANDO
                _save(report)
            return None
        report.state = FiscalReportState.EMITINDO
        _save(report)
    return _step(report, operation, key, attempt)


def _after_apply(report: FiscalSituationReport) -> _Step | None:
    """Go on to the next identity at once; the wait gate still applies to it."""
    if report.state in FINAL_STATES:
        return None
    operation, key, attempt = _next(report)
    if ProviderCall.objects.filter(
        client_id=report.client_id, request_key=key
    ).exists():
        return None
    return _first_call(report, operation, key, attempt)


def _decide(  # noqa: PLR0911 - each return names one A7.3 waiting point
    tenant_id: UUID, report_id: UUID
) -> _Step | None:
    """Apply the newest final call to the report, then name the next call, if any."""
    with tenant_context(tenant_id), transaction.atomic():
        report = _locked(report_id)
        _recover_pdf(report)
        if report.document_id:
            return _received_pdf(report)
        if report.state in FINAL_STATES:
            return None
        if report.state == FiscalReportState.SOLICITADO and report.protocol:
            # The protocol reply was recorded; its call may never have finalized.
            _accept_protocol(report)
        if report.state == FiscalReportState.PROTOCOLO_RECEBIDO:
            report.state = FiscalReportState.AGUARDANDO
            _save(report)
        operation, key, attempt = _next(report)
        call = ProviderCall.objects.filter(
            client_id=report.client_id, request_key=key
        ).first()
        now = timezone.now()
        if call is None:
            return _first_call(report, operation, key, attempt)
        if call.state in {CallState.RESERVED, CallState.RETRY}:
            return _first_call(report, operation, key, attempt)
        if call.state in {CallState.CLAIMED, CallState.STARTED}:
            if call.lease_until is not None and call.lease_until > now:
                return None
            # The claim quarantines the expired lease; the next pass applies it.
            return _step(report, operation, key, attempt)
        _apply(report, call, operation, attempt)
        return _after_apply(report)


def _apply(
    report: FiscalSituationReport,
    call: ProviderCall,
    operation: str,
    attempt: int,
) -> None:
    """Map one final call onto the report; the call row is the only input."""
    state = call.state
    if state == CallState.DENIED and call.http_status == 403:  # noqa: PLR2004
        record_authority_denial(
            report.client, operation, source_call_ref=str(call.pk), http_status=403
        )
    if operation == PROTOCOL_OPERATION:
        endings: dict[str, str] = {
            CallState.DENIED: FiscalReportState.NEGADO,
            CallState.AUTHORITY: FiscalReportState.NEGADO,
            CallState.UNCERTAIN: FiscalReportState.INCERTO,
        }
        ending = endings.get(state, FiscalReportState.FALHA)
        quota = (
            FederalConsultationOutcome.COTA_ESGOTADA
            if state == CallState.QUOTA
            else None
        )
        _finish(report, ending, operation, call, quota)
        return
    transmitted = call.attempts > 0
    if transmitted:
        report.emission_attempts = attempt
    pending = state in {CallState.WAITING, CallState.UNCERTAIN} or (
        state == CallState.SUCCESS and call.http_status == 204  # noqa: PLR2004
    )
    if pending:
        # Every emission identity counts toward the cap, sent or not: an identity
        # is never reused, so the report always reaches a final state.
        report.emission_attempts = attempt
        if attempt >= MAX_EMISSION_ATTEMPTS:
            _finish(report, FiscalReportState.LIMITE_DE_TENTATIVAS, operation, call)
            return
        if transmitted:
            wait = _stored_wait(report, call.pk, operation)
            deadline = timezone.now() + wait
            report.wait_until = max(deadline, _announced_until(report) or deadline)
        else:
            # Never sent, so nothing was paid and SERPRO asked for no wait: the
            # next identity may go at once (the existing wait_until has passed).
            logger.info(
                "SITFIS emission never sent; trying the next attempt",
                extra={"sitfis_event": "emissao_nao_enviada", "report": str(report.pk)},
            )
        report.state = FiscalReportState.AGUARDANDO
        _save(report)
        return
    if state in {CallState.DENIED, CallState.AUTHORITY}:
        ending = FiscalReportState.NEGADO
    elif state in {CallState.PAUSED, CallState.CANCELLED}:
        # A6 rollout: with the integration off, the in-flight protocol expires.
        ending = FiscalReportState.PROTOCOLO_EXPIRADO
    else:
        # A paid 200 whose PDF was not kept, quota, malformed replies, 4xx.
        ending = FiscalReportState.FALHA
    quota = (
        FederalConsultationOutcome.COTA_ESGOTADA if state == CallState.QUOTA else None
    )
    _finish(report, ending, operation, call, quota)


def _refused(
    tenant_id: UUID,
    report_id: UUID,
    step: _Step,
    error: AdmissionDeniedError,
    row_id: UUID | None,
) -> None:
    """End a report whose next call the live controls refused before sending."""
    with tenant_context(tenant_id), transaction.atomic():
        report = _locked(report_id)
        if row_id is not None:
            unused = (
                ProviderCall.objects.select_for_update(skip_locked=True)
                .filter(pk=row_id, state=CallState.RESERVED)
                .first()
            )
            if unused is not None:
                release(unused)
                unused.state = CallState.CANCELLED
                unused.provider_code = str(error)[:64]
                unused.version += 1
                unused.save()
        if report.state in FINAL_STATES or _next(report)[1] != step.key:
            return
        if str(error) == CallState.AUTHORITY:
            ending = FiscalReportState.NEGADO
        elif str(error) == CallState.PAUSED and step.operation == EMISSION_OPERATION:
            ending = FiscalReportState.PROTOCOLO_EXPIRADO
        else:
            ending = FiscalReportState.FALHA
        _finish(report, ending, step.operation, None)


def _conflicted(tenant_id: UUID, report_id: UUID, step: _Step) -> None:
    with tenant_context(tenant_id), transaction.atomic():
        report = _locked(report_id)
        if report.state not in FINAL_STATES and _next(report)[1] == step.key:
            _finish(report, FiscalReportState.FALHA, step.operation, None)


def _deferred(
    tenant_id: UUID,
    report_id: UUID,
    error: EmissionDeferredError,
) -> None:
    """Restore only our unstarted claim; no send, attempt or reservation is lost."""
    with report_mutex(tenant_id, report_id), tenant_context(tenant_id):
        call = ProviderCall.objects.select_for_update().filter(pk=error.call_id).first()
        if (
            call is not None
            and call.state == CallState.CLAIMED
            and call.fence == error.fence
            and call.lease_owner == error.owner
            and call.dispatch_started_at is None
        ):
            call.state = CallState.RESERVED
            call.lease_owner = None
            call.lease_until = None
            call.version += 1
            call.save()
        report = _locked(report_id)
        _recover_pdf(report)
        if report.document_id is not None:
            _received_pdf(report)
        elif report.state not in FINAL_STATES:
            deadline = _announced_until(report)
            if deadline is not None and (
                report.wait_until is None or deadline > report.wait_until
            ):
                report.wait_until = deadline
            report.state = FiscalReportState.AGUARDANDO
            _save(report)


def _run(tenant_id: UUID, report_id: UUID, step: _Step) -> None:
    """Make (or resume) the one call; each protocol phase owns its transaction."""
    body = build_request(step.operation, step.parties, step.data).body
    request = CallRequest(
        step.operation, step.key, hashlib.sha256(body).hexdigest(), step.key
    )
    row: ProviderCall | None = None
    ticket: Claim | None = None
    try:
        row = reserve(tenant_id, step.link_id, request)
        ticket = claim(tenant_id, row.pk)
        if ticket is None:
            return  # Another runner's live lease, refusal or quarantined lease.
        adapter = SitfisAdapter(
            SerproAdapter(
                _transport(cast("Environment", step.environment)),
                step.parties,
                step.data,
            ),
            tenant_id,
            report_id,
        )
        try:
            perform(ticket, adapter, keep=_keeper(report_id, step))
        except (AdmissionDeniedError, StaleClaimError):
            return  # Recorded on the call and applied by the next decision.
    except EmissionDeferredError as error:
        _deferred(tenant_id, report_id, error)
    except BudgetDeniedError:
        return  # The call row records the quota refusal; the next pass applies it.
    except AdmissionDeniedError as error:
        _refused(tenant_id, report_id, step, error, row.pk if row is not None else None)
    except IntentConflictError:
        _conflicted(tenant_id, report_id, step)


def advance_situation_report(tenant_id: UUID, report_id: UUID) -> FiscalSituationReport:
    """Run the report as far as the clock and the provider allow, then stop.

    Never raises for a provider outcome: every ending is recorded and owned. A
    runner that finds another's live lease leaves the report untouched.
    """
    assert_no_transaction()
    for _ in range(MAX_STEPS):
        step = _decide(tenant_id, report_id)
        if step is None:
            break
        _run(tenant_id, report_id, step)
    with tenant_context(tenant_id):
        return FiscalSituationReport.objects.get(pk=report_id)


def next_run_at(report: FiscalSituationReport) -> datetime | None:
    """When a delivery should look at this report again; None once it is final."""
    if report.state in FINAL_STATES:
        return None
    now = timezone.now()
    if report.state == FiscalReportState.AGUARDANDO and report.wait_until:
        return max(report.wait_until, now)
    with tenant_context(report.tenant_id):
        _operation, key, _attempt = _next(report)
        lease = (
            ProviderCall.objects.filter(client_id=report.client_id, request_key=key)
            .values_list("lease_until", flat=True)
            .first()
        )
    return max(lease or now, now) + LEASE_MARGIN


def own_stalled_report(tenant_id: UUID, report_id: UUID) -> None:
    """Own a report whose deliveries keep failing; its state is left as it is.

    Idempotent (raise_problem converges) and sends nothing: a later delivery that
    succeeds still continues the report from its durable rows.
    """
    with tenant_context(tenant_id), transaction.atomic():
        report = _locked(report_id)
        if report.state in FINAL_STATES:
            return
        operation = (
            PROTOCOL_OPERATION
            if report.state == FiscalReportState.SOLICITADO
            else EMISSION_OPERATION
        )
        _own(report, operation)
