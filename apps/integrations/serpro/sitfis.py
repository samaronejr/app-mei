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
* Every reply is decided before the protocol makes it final, and the protocol
  or the PDF is written in the same transaction that finalizes the call as a
  success, so nothing raises after finality and no paid result is lost. Every
  ending other than `emitido` opens owned work.

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
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Final, cast

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
from apps.cycles.models import CycleEvent, ServiceCycle
from apps.cycles.openers import CLOSED_STATES
from apps.cycles.scanning import scan_required
from apps.cycles.services.exceptions import ExceptionReason
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
    IntentConflictError,
    StaleClaimError,
)
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_protocol import claim, perform, reserve
from apps.integrations.models import ClientProviderLink, IssueReason, LinkStatus
from apps.integrations.serpro.adapter import SerproAdapter
from apps.integrations.serpro.codec import build_request, parse_response
from apps.integrations.serpro.parties import termo_parties
from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import TransportFailure
from apps.obligations.models import (
    AcquisitionOrigin,
    FederalConsultation,
    FederalConsultationKind,
    FederalConsultationOutcome,
    FiscalReportState,
    FiscalSituationReport,
)
from apps.obligations.models.federal import MAX_EMISSION_ATTEMPTS
from apps.obligations.system_ingest import ingest_system_document

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
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
# A provider estimate beyond one hour is not trusted; the default applies.
MAX_WAIT_MS: Final = 3_600_000
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
    if type(value) is not int or not 0 <= value <= MAX_WAIT_MS:
        return None
    return timedelta(milliseconds=value)


@dataclass(slots=True)
class SitfisReply:
    """Hold one call's transient, already-validated reply; never persisted."""

    protocol: str = field(default="", repr=False)
    wait: timedelta | None = None
    pdf: bytes | None = field(default=None, repr=False)


class SitfisAdapter:
    """Decide every SITFIS reply before the protocol makes it final.

    A malformed success is a failed call (billed by its HTTP status), never a
    success whose processing could raise after finalization.
    """

    def __init__(self, inner: SerproAdapter) -> None:
        """Wrap the AXRM-013 adapter; its transient response is read once here."""
        self.inner = inner
        self.reply = SitfisReply()

    def send(self, dispatch: Dispatch) -> CallResult:
        """Send once through the AXRM-013 adapter, then validate what came back."""
        self.reply = SitfisReply()
        result = self.inner.send(dispatch)
        response = self.inner.response
        if response is None:
            return result
        operation = dispatch.receipt.operation
        if operation == PROTOCOL_OPERATION and result.state == CallState.SUCCESS:
            return self._protocol(result, response)
        if operation == EMISSION_OPERATION:
            if result.state == CallState.SUCCESS and response.status == 200:  # noqa: PLR2004
                return self._report(result, response)
            if result.state == CallState.SUCCESS:
                self.reply.wait = _etag_wait(response)
            elif result.state == CallState.WAITING:
                self.reply.wait = _dados_wait(response)
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
        if not pdf.startswith(b"%PDF-") or len(pdf) > settings.PORTAL_UPLOAD_MAX_BYTES:
            return _invalid(result)
        self.reply.pdf = pdf
        return result


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


def _store_protocol(
    report: FiscalSituationReport, reply: SitfisReply, now: datetime
) -> None:
    report.protocol = reply.protocol
    report.protocol_date = now
    wait = reply.wait or _default_wait(report, PROTOCOL_OPERATION)
    report.wait_until = now + wait
    report.state = FiscalReportState.PROTOCOLO_RECEBIDO
    _save(report)


def _store_report(
    report: FiscalSituationReport, call: ProviderCall, pdf: bytes, attempt: int
) -> None:
    report.emission_attempts = attempt
    try:
        document = ingest_system_document(
            tenant_id=report.tenant_id,
            client_id=report.client_id,
            origin=AcquisitionOrigin.FEDERAL_PROVIDER,
            receipt_ref=str(call.pk),
            payload=pdf,
            filename=f"situacao-fiscal-{report.pk}.pdf",
            content_type="application/pdf",
            retention_class=RetentionClass.FISCAL_EVIDENCE,
        )
    except (UploadScannerOff, UploadRejected) as error:
        # Billed and never requested again automatically; owned for staff.
        logger.warning(
            "SITFIS report not stored",
            extra={
                "sitfis_event": "relatorio_nao_armazenado",
                "code": type(error).__name__,
            },
        )
        _finish(report, FiscalReportState.FALHA, EMISSION_OPERATION, call)
        return
    report.document = document
    report.emitted_at = call.finalized_at or timezone.now()
    _finish(report, FiscalReportState.EMITIDO, EMISSION_OPERATION, call)
    _emitted_event(report)


def _keeper(
    report_id: UUID, step: _Step, adapter: SitfisAdapter
) -> Callable[[ProviderCall], None]:
    """Write the validated reply in the transaction that finalizes the success."""

    def keep(call: ProviderCall) -> None:
        report = FiscalSituationReport.objects.select_for_update().get(pk=report_id)
        if call.request_key != step.key:
            return
        reply = adapter.reply
        if step.operation == PROTOCOL_OPERATION:
            if reply.protocol and report.state == FiscalReportState.SOLICITADO:
                _store_protocol(report, reply, call.finalized_at or timezone.now())
        elif (
            reply.pdf is not None
            and report.state == FiscalReportState.EMITINDO
            and report.emission_attempts + 1 == step.attempt
        ):
            _store_report(report, call, reply.pdf, step.attempt)

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


def _decide(  # noqa: PLR0911 - each return names one A7.3 waiting point
    tenant_id: UUID, report_id: UUID, waits: dict[UUID, timedelta | None]
) -> _Step | None:
    """Apply the newest final call to the report, then name the next call, if any."""
    with tenant_context(tenant_id), transaction.atomic():
        report = _locked(report_id)
        if report.state in FINAL_STATES:
            return None
        if report.state == FiscalReportState.PROTOCOLO_RECEBIDO:
            report.state = FiscalReportState.AGUARDANDO
            _save(report)
        operation, key, attempt = _next(report)
        call = ProviderCall.objects.filter(
            client_id=report.client_id, request_key=key
        ).first()
        now = timezone.now()
        if call is None:
            if operation == EMISSION_OPERATION:
                if report.wait_until is not None and now < report.wait_until:
                    if report.state != FiscalReportState.AGUARDANDO:
                        report.state = FiscalReportState.AGUARDANDO
                        _save(report)
                    return None
                report.state = FiscalReportState.EMITINDO
                _save(report)
            return _step(report, operation, key, attempt)
        if call.state == CallState.RESERVED:
            return _step(report, operation, key, attempt)
        if call.state in {CallState.CLAIMED, CallState.STARTED}:
            if call.lease_until is not None and call.lease_until > now:
                return None
            # The claim quarantines the expired lease; the next pass applies it.
            return _step(report, operation, key, attempt)
        _apply(report, call, operation, attempt, waits)
        return None


def _apply(
    report: FiscalSituationReport,
    call: ProviderCall,
    operation: str,
    attempt: int,
    waits: dict[UUID, timedelta | None],
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
        if attempt >= MAX_EMISSION_ATTEMPTS:
            _finish(report, FiscalReportState.LIMITE_DE_TENTATIVAS, operation, call)
            return
        wait = waits.get(call.pk) or _default_wait(report, operation)
        report.wait_until = timezone.now() + wait
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


def _run(
    tenant_id: UUID,
    report_id: UUID,
    step: _Step,
    waits: dict[UUID, timedelta | None],
) -> None:
    """Make (or resume) the one call; each protocol phase owns its transaction."""
    body = build_request(step.operation, step.parties, step.data).body
    request = CallRequest(
        step.operation, step.key, hashlib.sha256(body).hexdigest(), step.key
    )
    try:
        row = reserve(tenant_id, step.link_id, request)
    except BudgetDeniedError:
        return  # The call row records the quota refusal; the next pass applies it.
    except AdmissionDeniedError as error:
        _refused(tenant_id, report_id, step, error, None)
        return
    except IntentConflictError:
        _conflicted(tenant_id, report_id, step)
        return
    try:
        ticket = claim(tenant_id, row.pk)
    except AdmissionDeniedError as error:
        _refused(tenant_id, report_id, step, error, row.pk)
        return
    if ticket is None:
        return  # Another runner's live lease, a quota refusal or a quarantined lease.
    adapter = SitfisAdapter(
        SerproAdapter(
            _transport(cast("Environment", step.environment)),
            step.parties,
            step.data,
        )
    )
    try:
        perform(ticket, adapter, keep=_keeper(report_id, step, adapter))
    except (AdmissionDeniedError, StaleClaimError):
        return  # Recorded on the call (pausada, bloqueada_*, incerta); applied next.
    waits[row.pk] = adapter.reply.wait


def advance_situation_report(tenant_id: UUID, report_id: UUID) -> FiscalSituationReport:
    """Run the report as far as the clock and the provider allow, then stop.

    Never raises for a provider outcome: every ending is recorded and owned. A
    runner that finds another's live lease leaves the report untouched.
    """
    assert_no_transaction()
    waits: dict[UUID, timedelta | None] = {}
    for _ in range(MAX_STEPS):
        step = _decide(tenant_id, report_id, waits)
        if step is None:
            break
        _run(tenant_id, report_id, step, waits)
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
        operation, key, _attempt = _next(report)
        lease = (
            ProviderCall.objects.filter(client_id=report.client_id, request_key=key)
            .values_list("lease_until", flat=True)
            .first()
        )
    del operation
    return max(lease or now, now) + LEASE_MARGIN
