"""The official MEI DAS guide (PGMEI `GERARDASPDF21`) on staff request (AXRM-017).

A6 SP-DAS: an issuance billed per emission, never scheduled and never a payment. The
guide reaches the client only through the existing exact-approved publication; this
module stops at a firm-only request carrying the scanned, private original.

Every call runs through the AXRM-013 adapter, the AXRM-009 phased protocol (claim,
fence, journal) and the AXRM-010 reservation. The reply is decided before the call
becomes final: only a complete, attributable PDF for this taxpayer and period is a
success, and its bytes are stored (quarantined, `portal_visible=False`) before the
fenced finalization. Everything after finality is idempotent and converges on owned
work instead of raising: a linking failure or a definitive refusal opens a registered
exception, an ambiguous reply is AXRM-009's `chamada_provedor_incerta`, and the
daily evaluator settles any call a dead worker left unsettled.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Q
from django.http import Http404
from django.utils import timezone

from apps.audit.models import Event
from apps.audit.services import ObjectRef, record_event
from apps.authz.services import can
from apps.clients.services import profile_at
from apps.core.flags import tenant_has
from apps.core.retention import RetentionClass
from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.cycles.errors import IllegalTransition, VersionConflict
from apps.cycles.evidence import (
    EvidenceLink,
    EvidenceRequest,
    LinkOrigin,
    RequestInternal,
    RequestState,
)
from apps.cycles.intake import (
    UploadMissing,
    UploadRejected,
    UploadScannerOff,
    UploadTooLarge,
)
from apps.cycles.models import CycleEvent, ServiceCycle
from apps.cycles.services.cycle import OPEN_STATES, load_cycle
from apps.cycles.services.exceptions import ExceptionReason
from apps.integrations.authority import Problem, raise_problem
from apps.integrations.budget_pricing import BudgetDeniedError, microbrl
from apps.integrations.budget_settlement import release
from apps.integrations.call_contracts import (
    AdmissionDeniedError,
    CallRequest,
    CallResult,
    Claim,
    IntentConflictError,
    StaleClaimError,
)
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_protocol import claim, perform, reserve
from apps.integrations.call_state import lock_controls, uncertain
from apps.integrations.models import (
    ClientProviderLink,
    IntegrationIssue,
)
from apps.integrations.serpro.adapter import SerproAdapter
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
from apps.integrations.serpro.termo_state import termo_head, termo_in_force
from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import (
    Environment,
    JSONValue,
    TransportFailure,
)
from apps.obligations.federal import verifies
from apps.obligations.models import (
    AcquisitionOrigin,
    Document,
    Obligation,
    ObservationKind,
    ObservationSourceKind,
    ObservationVerificationStatus,
    SourceObservation,
)
from apps.obligations.models.observations import ObservationOutcome
from apps.obligations.observations import assert_fact, lock_subjects
from apps.obligations.system_ingest import (
    InvalidSystemDocument,
    ingest_system_document,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from apps.accounts.models import User
    from apps.clients.models import TaxProfile
    from apps.integrations.call_dispatch import Dispatch
    from apps.tenants.models import Tenant

__all__ = ["IllegalTransition"]

OPERATION: Final = "SP-DAS"
CAPABILITY: Final = "das.generate"
GUIDE_TITLE: Final = "Guia DAS oficial"
GUIDE_FILENAME_PREFIX: Final = "guia-das-"
FIRM_ONLY_STATE: Final = RequestState.INTERNAL
REQUESTED_EVENT: Final = "integrations.das_guide_requested"
GUIDE_NOT_OBTAINED: Final = "guia_das_nao_obtida"
GUIDE_NOT_LINKED: Final = "guia_das_nao_vinculada"
# A request refused before any I/O (quota, pause, authority) sent nothing and set no
# effect barrier, so the same day may ask again under a new key, a bounded number
# of times. A request that reached dispatch is never repeated the same day.
MAX_DAILY_REQUESTS: Final = 5
# One exhaustive classification for both client and firm protocol states. Every
# non-final call blocks a new paid click until it is released, linked or owned.
# The state-walk test checks this against both models' choices and CallState.
CALL_FINAL: Final[dict[str, bool]] = {
    CallState.QUEUED: False,
    CallState.RESERVED: False,
    CallState.CLAIMED: False,
    CallState.STARTED: False,
    CallState.SUCCESS: True,
    CallState.WAITING: False,
    CallState.RETRY: False,
    CallState.FAILED: True,
    CallState.DENIED: True,
    CallState.UNCERTAIN: True,
    CallState.RECONCILED: True,
    CallState.QUOTA: True,
    CallState.AUTHORITY: True,
    CallState.PAUSED: True,
    CallState.CANCELLED: True,
}
# reserve() commits before claim() creates a lease. Give that runner the same
# 120-second handoff as a claimed runner, rather than releasing its live intent.
UNCLAIMED_GRACE: Final = timedelta(seconds=120)
REFUSED_CODES: Final[dict[str, str]] = {
    CallState.QUOTA: "cota_esgotada",
    CallState.PAUSED: "provedor_pausado",
    CallState.AUTHORITY: "sem_autorizacao",
    CallState.CANCELLED: "cancelada",
}
REASONS: Final = {
    GUIDE_NOT_OBTAINED: ExceptionReason(
        "Confira a recusa da Receita para a guia DAS oficial. Corrija a causa e "
        "solicite de novo em outro dia, ou gere a guia pelo PGMEI.",
        blocks_completion=False,
        label="guia DAS oficial não obtida",
    ),
    GUIDE_NOT_LINKED: ExceptionReason(
        "A guia DAS oficial foi emitida, mas não pôde ser anexada ao ciclo. "
        "Confira o ciclo e a guia guardada antes de publicar.",
        blocks_completion=False,
        label="guia DAS oficial não anexada",
    ),
}
REFUSAL_LABELS: Final = {
    "sem_permissao": "Sua função não pode obter guias DAS.",
    "sinalizador_desligado": "As consultas federais não estão ativas neste escritório.",
    "ciclo_encerrado": "O ciclo está encerrado.",
    "servico_nao_mei": "Este ciclo não é de MEI mensal.",
    "nao_mei": "O cliente não é MEI nesta competência.",
    "sem_vinculo_serpro": "O cliente não tem vínculo ativo com o SERPRO.",
    "termo_ausente": "O termo de autorização do escritório não está em vigor.",
    "cota_esgotada": "A cota mensal do SERPRO está esgotada; nada foi enviado.",
    "provedor_pausado": "O SERPRO está pausado; nada foi enviado.",
    "sem_autorizacao": "A autorização para o SERPRO não está válida; nada foi enviado.",
    "pedido_divergente": "Já existe um pedido diferente para esta guia hoje.",
    "tarifa_ausente": "O preço da emissão não está configurado.",
    "cancelada": "O pedido foi cancelado antes do envio.",
    "limite_diario": "O limite de pedidos desta guia hoje foi atingido.",
}
OUTCOME_LABELS: Final = {
    "guia_obtida": "Guia obtida",
    "guia_existente": "Guia obtida",
    "em_andamento": "Pedido em andamento",
    "guia_nao_obtida": "Guia não obtida",
    "guia_nao_vinculada": "Guia emitida, não anexada",
    "resultado_incerto": "Resultado incerto",
    "guia_recuperada": "Guia recuperada de um pedido anterior",
}


class GuideRefused(Exception):  # noqa: N818 - domain vocabulary
    """A guide request refused before any provider I/O, with a minimized code."""

    def __init__(self, code: str) -> None:
        """Carry the code only; labels live in `REFUSAL_LABELS`."""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class GuideOutcome:
    """Identifiers and a code; never guide bytes, amounts or taxpayer data."""

    code: str
    call_id: UUID
    document_id: UUID | None = None
    link_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class _Prepared:
    cycle_id: UUID
    client_id: UUID
    link_id: UUID
    parties: Parties
    environment: Environment
    data: JSONValue
    request: CallRequest
    competence: str
    day: date


def consolidation_day() -> date:
    """Return the local consolidation date, also the daily idempotency key."""
    return timezone.localdate()


def fake_origin() -> str:
    """Return the loopback origin of the fake environment, if configured."""
    return str(getattr(settings, "SERPRO_FAKE_ORIGIN", ""))


def competence(cycle: ServiceCycle) -> str:
    """PGMEI's `periodoApuracao` (AAAAMM) is the cycle's competence month."""
    return cycle.period_start.strftime("%Y%m")


def estimated_cost_microbrl() -> int:
    """Price one emission as the budget will reserve it; refuse without a tariff."""
    raw = settings.USAGE_UNIT_PRICES_BRL.get("serpro_emissao")
    if raw is None:
        code = "tarifa_ausente"
        raise GuideRefused(code)
    try:
        return microbrl(raw)
    except BudgetDeniedError:
        code = "tarifa_ausente"
        raise GuideRefused(code) from None


def brl(value: int) -> str:
    """Render micro-BRL as pt-BR currency text for the preview."""
    cents = value // 10_000
    return f"R$ {cents // 100:,}".replace(",", ".") + f",{cents % 100:02d}"


def _serpro_link(client_id: UUID) -> ClientProviderLink | None:
    return (
        ClientProviderLink.objects.select_related("connection")
        .filter(
            client_id=client_id,
            status="ativa",
            disablement_state="ativa",
            connection__provider="serpro",
            connection__status="ativa",
            connection__environment=settings.SERPRO_ENVIRONMENT,
        )
        .order_by("pk")
        .first()
    )


def guide_eligibility(actor: User, cycle: ServiceCycle) -> str:  # noqa: PLR0911 - one code per gate
    """Why the action is unavailable for this cycle, or "" (caller's tenant context)."""
    if not can(actor, CAPABILITY, tenant_id=cycle.tenant_id):
        return "sem_permissao"
    if not tenant_has(cycle.tenant_id, "federal_checks"):
        return "sinalizador_desligado"
    if cycle.state not in OPEN_STATES:
        return "ciclo_encerrado"
    if cycle.engagement.service_kind != "mei_mensal":
        return "servico_nao_mei"
    profile = profile_at(cycle.client, cycle.period_start)
    if profile is None or profile.regime != "mei":
        return "nao_mei"
    if _serpro_link(cycle.client_id) is None:
        return "sem_vinculo_serpro"
    return ""


def _parties(link: ClientProviderLink) -> Parties | None:
    """Contratante and firm exactly as the termo in force names them (INV-R1)."""
    connection_id = link.connection_id
    if not termo_in_force(connection_id, timezone.localdate()):
        return None
    head = termo_head(connection_id)
    if head is None:
        return None
    try:
        raw = stored_termo(head.signed_document)
        names = _termo_names(raw)
        if names is None:
            return None
        contratante, firm = names
        read_signed_termo(raw, Parties(contratante, firm, firm))
        return Parties(contratante, firm, link.client.cnpj)
    except (TermoInvalidoError, CnpjInvalidoError):
        return None


def _termo_names(raw: bytes) -> tuple[str, str] | None:
    from defusedxml import ElementTree  # noqa: PLC0415
    from defusedxml.common import DefusedXmlException  # noqa: PLC0415

    try:
        data = ElementTree.fromstring(
            raw, forbid_dtd=True, forbid_entities=True, forbid_external=True
        ).find("dados")
    except (ElementTree.ParseError, DefusedXmlException):
        return None
    if data is None:
        return None
    recipient, author = data.find("destinatario"), data.find("assinadoPor")
    if recipient is None or author is None:
        return None
    return recipient.get("numero", ""), author.get("numero", "")


def _environment(link: ClientProviderLink) -> Environment:
    match link.connection.environment:
        case "fake":
            return "fake"
        case "trial":
            return "trial"
        case "producao":
            return "producao"
    code = "sem_vinculo_serpro"
    raise GuideRefused(code)


def _prepare(actor: User, tenant_id: UUID, cycle_id: UUID) -> _Prepared:
    """Authorize, check eligibility and freeze the canonical request identity."""
    if not can(actor, CAPABILITY, tenant_id=tenant_id):
        raise PermissionDenied
    cycle = load_cycle(actor, cycle_id)
    reason = guide_eligibility(actor, cycle)
    if reason:
        raise GuideRefused(reason)
    link = _serpro_link(cycle.client_id)
    if link is None:
        code = "sem_vinculo_serpro"
        raise GuideRefused(code)
    parties = _parties(link)
    if parties is None:
        code = "termo_ausente"
        raise GuideRefused(code)
    environment = _environment(link)
    day = consolidation_day()
    period = competence(cycle)
    data: JSONValue = {
        "periodoApuracao": period,
        "dataConsolidacao": day.strftime("%Y%m%d"),
    }
    wire = build_request(OPERATION, parties, data)
    identity = f"{cycle.client_id}:{period}:{day.isoformat()}"
    request = CallRequest(
        OPERATION,
        _request_key(cycle.client_id, identity),
        hashlib.sha256(wire.body).hexdigest(),
        hashlib.sha256(
            f"{tenant_id}:{environment}:{OPERATION}:{identity}".encode()
        ).hexdigest(),
    )
    return _Prepared(
        cycle.pk,
        cycle.client_id,
        link.pk,
        parties,
        environment,
        data,
        request,
        period,
        day,
    )


def _request_key(client_id: UUID, identity: str) -> str:
    """Today's key, advanced only past requests refused before any I/O."""
    for sequence in range(MAX_DAILY_REQUESTS):
        key = hashlib.sha256(f"pgmei-guide:{identity}:{sequence}".encode()).hexdigest()
        previous = ProviderCall.objects.filter(
            client_id=client_id, request_key=key
        ).first()
        if (
            previous is None
            or previous.state not in REFUSED_CODES
            or previous.attempts > 0
            or previous.dispatch_started_at is not None
        ):
            return key
    code = "limite_diario"
    raise GuideRefused(code)


@dataclass(frozen=True, slots=True)
class _Guide:
    pdf: bytes
    numero_documento: str


def _decode_guide(  # noqa: PLR0911 - every refusal is the same None
    adapter: SerproAdapter, client_cnpj: str, period: str
) -> _Guide | None:
    """Accept exactly one documented `Das` naming this taxpayer and period."""
    response = adapter.response
    if response is None or response.status != 200:  # noqa: PLR2004 - documented
        return None
    try:
        items = parse_response(response).data
    except TransportFailure:
        return None
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return None
    item = items[0]
    detail = item.get("detalhamento")
    if isinstance(detail, list) and len(detail) == 1:
        detail = detail[0]
    encoded = item.get("pdf")
    if (
        not isinstance(detail, dict)
        or not isinstance(encoded, str)
        or str(item.get("cnpjCompleto", "")).upper() != client_cnpj
        or detail.get("periodoApuracao") != period
    ):
        return None
    try:
        pdf = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not pdf.startswith(b"%PDF-"):
        return None
    numero = detail.get("numeroDocumento")
    return _Guide(pdf, numero if isinstance(numero, str) else "")


@dataclass(slots=True)
class GuideAdapter:
    """Decide the reply and persist its protected PDF before the call is final."""

    inner: SerproAdapter
    tenant_id: UUID
    client_id: UUID
    period: str
    day: date
    document_id: UUID | None = None

    def send(self, dispatch: Dispatch) -> CallResult:
        """Only a stored, attributable guide is a success; anything else is owned."""
        self.document_id = None
        result = self.inner.send(dispatch)
        if result.state in {CallState.FAILED, CallState.DENIED, CallState.UNCERTAIN}:
            return result
        guide = _decode_guide(self.inner, self.inner.parties.client, self.period)
        if result.state != CallState.SUCCESS or guide is None:
            return CallResult(
                CallState.UNCERTAIN,
                result.http_status,
                "resposta_guia_invalida",
                result.billed,
                result.response_digest,
            )
        try:
            document = ingest_system_document(
                tenant_id=self.tenant_id,
                client_id=self.client_id,
                origin=AcquisitionOrigin.FEDERAL_PROVIDER,
                receipt_ref=str(dispatch.claim.call_id),
                payload=guide.pdf,
                filename=f"{GUIDE_FILENAME_PREFIX}{self.period}-{self.day:%Y%m%d}.pdf",
                content_type="application/pdf",
                retention_class=RetentionClass.FISCAL_EVIDENCE,
            )
        except (
            UploadTooLarge,
            UploadMissing,
            UploadRejected,
            UploadScannerOff,
            InvalidSystemDocument,
            DatabaseError,
            OSError,
        ):
            return CallResult(
                CallState.UNCERTAIN,
                result.http_status,
                "guia_nao_armazenada",
                result.billed,
                result.response_digest,
            )
        self.document_id = document.pk
        return result


def _refusal(error: AdmissionDeniedError) -> GuideRefused:
    if isinstance(error, BudgetDeniedError):
        return GuideRefused("cota_esgotada")
    if str(error) == CallState.PAUSED:
        return GuideRefused("provedor_pausado")
    return GuideRefused("sem_autorizacao")


def obtain_das_guide(
    actor: User, tenant_id: UUID, cycle_id: UUID, *, origin: str | None = None
) -> GuideOutcome:
    """Obtain today's guide once; a second request the same day reuses it."""
    assert_no_transaction()
    with tenant_context(tenant_id):
        prepared = _prepare(actor, tenant_id, cycle_id)
        existing = ProviderCall.objects.filter(
            client_id=prepared.client_id, request_key=prepared.request.request_key
        ).first()
        if existing is None:
            # Recorded before any reservation: who asked, for which cycle. The
            # daily evaluator links a guide a dead worker left behind with it.
            record_event(
                action=REQUESTED_EVENT,
                actor=actor,
                tenant_id=tenant_id,
                obj=ObjectRef("ServiceCycle", str(prepared.cycle_id)),
                metadata={"request_key": prepared.request.request_key},
            )
    earlier = _earlier_outcome(prepared, actor, tenant_id)
    if earlier is not None:
        return earlier
    try:
        call = reserve(tenant_id, prepared.link_id, prepared.request)
    except AdmissionDeniedError as error:
        raise _refusal(error) from None
    except IntentConflictError:
        code = "pedido_divergente"
        raise GuideRefused(code) from None
    # Two local-day runners can both pass the first check before either reserves.
    # Once durable, only the oldest reservation may proceed. A later reservation
    # waits on its predecessor and is eventually released if its runner abandons it.
    earlier = _earlier_outcome(prepared, actor, tenant_id, before=call)
    if earlier is not None:
        return earlier
    try:
        ticket = claim(tenant_id, call.pk)
    except AdmissionDeniedError as error:
        raise _refusal(error) from None
    if ticket is not None:
        _send(
            ticket,
            prepared,
            actor=actor,
            tenant_id=tenant_id,
            call_id=call.pk,
            origin=origin,
        )
    code = settle_guide(tenant_id, call.pk, cycle_id=prepared.cycle_id, actor=actor)
    if code in REFUSED_CODES.values():
        raise GuideRefused(code)
    if code == "guia_obtida" and existing is not None:
        code = "guia_existente"
    with tenant_context(tenant_id):
        document = _guide_document(call)
        link = (
            EvidenceLink.objects.filter(
                document=document, request__cycle_id=prepared.cycle_id
            ).first()
            if document is not None
            else None
        )
    return GuideOutcome(
        code,
        call.pk,
        document.pk if document is not None else None,
        link.pk if link is not None else None,
    )


def _send(  # noqa: PLR0913 - one claimed attempt and its frozen request
    ticket: Claim,
    prepared: _Prepared,
    *,
    actor: User,
    tenant_id: UUID,
    call_id: UUID,
    origin: str | None,
) -> None:
    """Perform the claimed attempt; a pre-send refusal settles and is re-raised."""
    adapter = GuideAdapter(
        SerproAdapter(
            ProviderTransport(
                "serpro",
                prepared.environment,
                (fake_origin() if origin is None else origin)
                if prepared.environment == "fake"
                else "",
            ),
            prepared.parties,
            prepared.data,
        ),
        tenant_id,
        prepared.client_id,
        prepared.competence,
        prepared.day,
    )
    try:
        perform(ticket, adapter)
    except AdmissionDeniedError as error:
        refused = _refusal(error)
        settle_guide(tenant_id, call_id, cycle_id=prepared.cycle_id, actor=actor)
        raise refused from None
    except StaleClaimError:
        # Lost before the first byte or quarantined as uncertain: settle reports it.
        return


def _lease_expired(call: ProviderCall) -> bool:
    return not CALL_FINAL[call.state] and (
        (call.lease_until or call.updated_at + UNCLAIMED_GRACE) <= timezone.now()
    )


def _recover(tenant_id: UUID, call: ProviderCall) -> None:
    """Release an unclaimed intent or own a call whose worker disappeared.

    Same lock order as claim(): invalidate a never-claimed reservation and return
    its budget in the same transaction. Every other abandoned non-final state is
    AXRM-009 uncertain work. A definitive-401 RETRY is also owned, never resumed
    automatically by this sweep; its settled unbilled attempt stays released.
    The live protocol may still retry that 401 once under its fresh claim/fence.
    """
    with tenant_context(tenant_id):
        lock_controls(tenant_id, call.link_id)
        locked = ProviderCall.objects.select_for_update().get(pk=call.pk)
        if _lease_expired(locked):
            locked.link = ClientProviderLink.objects.get(pk=locked.link_id)
            if (
                locked.fence == 0
                and locked.lease_owner is None
                and locked.attempts == 0
                and locked.dispatch_started_at is None
            ):
                locked.fence += 1
                locked.state = CallState.CANCELLED
                locked.billed = "nao"
                locked.settled_microbrl = 0
                locked.finalized_at = timezone.now()
                locked.updated_at = locked.finalized_at
                locked.version += 1
                locked.save()
                release(locked)
            else:
                uncertain(locked)


def _unsettled(call: ProviderCall) -> bool:
    """Whether a guide call still needs a link or owned work (caller's context)."""
    if not CALL_FINAL[call.state]:
        return True
    if call.state in {CallState.SUCCESS, CallState.UNCERTAIN}:
        document = _guide_document(call)
        if document is None:
            return call.state == CallState.SUCCESS and not _owned(
                call, GUIDE_NOT_LINKED
            )
        return not (
            EvidenceLink.objects.filter(document=document).exists()
            or _owned(call, GUIDE_NOT_LINKED)
        )
    if call.state in {CallState.FAILED, CallState.DENIED, CallState.AUTHORITY}:
        return not _owned(call, GUIDE_NOT_OBTAINED)
    return False


def _earlier_outcome(
    prepared: _Prepared,
    actor: User,
    tenant_id: UUID,
    *,
    before: ProviderCall | None = None,
) -> GuideOutcome | None:
    """Rule: never emit again while an earlier emission for the competence is open.

    An earlier guide call for the same client and competence (another day's key)
    that is still in flight with a live lease answers `em_andamento`; one that is
    unsettled (expired lease, stored PDF not adopted, refusal not owned) is settled
    by this click instead of a new emission. Only once every earlier emission is
    linked or owned may a click buy a new guide.
    """
    with tenant_context(tenant_id):
        cycle = ServiceCycle.objects.get(pk=prepared.cycle_id)
        cycles = ServiceCycle.objects.filter(
            client_id=cycle.client_id, period_start=cycle.period_start
        ).values_list("pk", flat=True)
        keys = set(
            Event.objects.filter(
                action=REQUESTED_EVENT,
                object_type="ServiceCycle",
                object_id__in=[str(pk) for pk in cycles],
            ).values_list("metadata__request_key", flat=True)
        ) - {prepared.request.request_key}
        earlier = ProviderCall.objects.filter(
            client_id=cycle.client_id, operation=OPERATION, request_key__in=keys
        )
        if before is not None:
            earlier = earlier.filter(
                Q(created_at__lt=before.created_at)
                | Q(created_at=before.created_at, pk__lt=before.pk)
            )
        earlier = earlier.order_by("created_at", "pk")
        pending = [call for call in earlier if _unsettled(call)]
    for call in pending:
        if not CALL_FINAL[call.state] and not _lease_expired(call):
            return GuideOutcome("em_andamento", call.pk)
    for call in pending:
        code = settle_guide(tenant_id, call.pk, cycle_id=prepared.cycle_id, actor=actor)
        if code == "guia_obtida":
            code = "guia_recuperada"
        with tenant_context(tenant_id):
            document = _guide_document(call)
            link = (
                EvidenceLink.objects.filter(
                    document=document, request__cycle_id=prepared.cycle_id
                ).first()
                if document is not None
                else None
            )
        return GuideOutcome(
            code,
            call.pk,
            document.pk if document is not None else None,
            link.pk if link is not None else None,
        )
    return None


def _guide_document(call: ProviderCall) -> Document | None:
    return Document.objects.filter(
        client_id=call.client_id,
        acquisition_origin=AcquisitionOrigin.FEDERAL_PROVIDER,
        acquisition_receipt_ref=str(call.pk),
    ).first()


def _requested(call: ProviderCall) -> tuple[UUID, UUID] | None:
    """Return the cycle and person behind a request key, from its first event."""
    event = (
        Event.objects.filter(
            action=REQUESTED_EVENT,
            object_type="ServiceCycle",
            metadata__request_key=call.request_key,
            actor__isnull=False,
        )
        .order_by("created_at", "pk")
        .first()
    )
    if event is None or event.actor_id is None:
        return None
    return UUID(event.object_id), event.actor_id


def _guide_request(cycle: ServiceCycle, actor: User) -> EvidenceRequest:
    """Reuse the cycle's firm-only guide request, or create it (until todo 29)."""
    request = (
        EvidenceRequest.objects.select_for_update()
        .filter(cycle=cycle, title=GUIDE_TITLE, state=FIRM_ONLY_STATE)
        .order_by("created_at", "pk")
        .first()
    )
    if request is not None:
        return request
    request = EvidenceRequest.objects.create(
        tenant_id=cycle.tenant_id,
        client_id=cycle.client_id,
        cycle=cycle,
        title=GUIDE_TITLE,
        required=False,
        state=FIRM_ONLY_STATE,
        created_by=actor,
    )
    RequestInternal.objects.create(
        tenant_id=cycle.tenant_id,
        client_id=cycle.client_id,
        request=request,
        instructions=(
            "Guia emitida pelo PGMEI (SERPRO). Revise o arquivo e publique pela "
            "etapa Guia DAS do mês, com aprovação."
        ),
    )
    cycle.version += 1
    cycle.save(update_fields=["version"])
    CycleEvent.objects.create(
        tenant_id=cycle.tenant_id,
        client_id=cycle.client_id,
        cycle=cycle,
        actor=actor,
        type="request.created",
        aggregate_type="EvidenceRequest",
        aggregate_id=request.pk,
        aggregate_version=request.version,
        payload={"origin": "pgmei"},
    )
    return request


def _link_in_cycle(
    cycle_id: UUID, document: Document, actor: User, call: ProviderCall
) -> tuple[ServiceCycle, EvidenceLink]:
    """Attach the stored guide to the firm-only request under the cycle lock."""
    cycle = ServiceCycle.objects.select_for_update().get(pk=cycle_id)
    if cycle.client_id != call.client_id or cycle.state not in OPEN_STATES:
        msg = "Este ciclo está encerrado."
        raise IllegalTransition(msg)
    request = _guide_request(cycle, actor)
    link, created = EvidenceLink.objects.get_or_create(
        tenant_id=cycle.tenant_id,
        client_id=cycle.client_id,
        request=request,
        document=document,
        defaults={"origin": LinkOrigin.STAFF, "submitted_by": actor},
    )
    if created:
        CycleEvent.objects.create(
            tenant_id=cycle.tenant_id,
            client_id=cycle.client_id,
            cycle=cycle,
            actor=actor,
            type="federal.guide.linked",
            aggregate_type="EvidenceRequest",
            aggregate_id=request.pk,
            aggregate_version=request.version,
            payload={
                "call_id": str(call.pk),
                "document_id": str(document.pk),
                "link_id": str(link.pk),
            },
        )
    return cycle, link


def _observe(
    call: ProviderCall, cycle: ServiceCycle, document: Document, link: EvidenceLink
) -> None:
    """Production only (INV-R4): one official guide-issued fact per call."""
    if not verifies(call):
        return
    period = cycle.period_start
    obligation = Obligation.objects.filter(
        client_id=call.client_id, obligation_type_id="DAS", competence_month=period
    ).first()
    if obligation is None:
        return
    lock_subjects([obligation.pk], client_id=call.client_id)
    if SourceObservation.objects.filter(
        subject_ref_id=obligation.pk,
        kind=ObservationKind.GUIDE_ISSUED,
        source_ref=str(call.pk),
    ).exists():
        return
    if call.finalized_at is None:
        return
    assert_fact(
        tenant_id=call.tenant_id,
        client_id=call.client_id,
        subject_id=obligation.pk,
        kind=ObservationKind.GUIDE_ISSUED,
        period=period,
        source_kind=ObservationSourceKind.OFFICIAL_API,
        observed_at=call.finalized_at,
        value={"sha256": document.sha256, "periodo_apuracao": competence(cycle)},
        verification_status=ObservationVerificationStatus.VERIFIED,
        outcome=ObservationOutcome.ACCEPTED,
        source_ref=str(call.pk),
        evidence_link_id=link.pk,
    )


def _owned(call: ProviderCall, reason: str) -> bool:
    if IntegrationIssue.objects.filter(
        client_id=call.client_id,
        reason=reason,
        evidence_refs__contains=[str(call.pk)],
    ).exists():
        return True
    from apps.cycles.models import Exception as CycleException  # noqa: PLC0415

    return CycleException.objects.filter(
        client_id=call.client_id, reason_code=reason, subject_ref=call.pk
    ).exists()


def _own(call: ProviderCall, reason: str) -> None:
    """Open the registered problem once; a resolved one is never reopened."""
    if _owned(call, reason):
        return
    raise_problem(
        call.client,
        Problem(
            reason,
            call.pk,
            review_on=timezone.localdate(),
            service=OPERATION,
            link=call.link,
        ),
    )


def settle_guide(
    tenant_id: UUID,
    call_id: UUID,
    *,
    cycle_id: UUID | None = None,
    actor: User | None = None,
) -> str:
    """Converge one guide call on a linked guide or owned work; never raise on it.

    Every abandoned non-final call is released or owned under its fence, and a PDF
    it already stored is adopted. No recovery path automatically sends anything.
    """
    with tenant_context(tenant_id):
        call = ProviderCall.objects.get(pk=call_id, operation=OPERATION)
    if _lease_expired(call):
        _recover(tenant_id, call)
    with tenant_context(tenant_id):
        call = ProviderCall.objects.select_related("client", "link").get(
            pk=call_id, operation=OPERATION
        )
        if not CALL_FINAL[call.state]:
            return "em_andamento"
        if call.state in REFUSED_CODES:
            if call.state == CallState.AUTHORITY:
                _own(call, GUIDE_NOT_OBTAINED)
            return REFUSED_CODES[call.state]
        if call.state == CallState.UNCERTAIN:
            if _guide_document(call) is None:
                return "resultado_incerto"
            code = _settle_success(call, cycle_id, actor)
            return "guia_recuperada" if code == "guia_obtida" else code
        if call.state != CallState.SUCCESS:
            _own(call, GUIDE_NOT_OBTAINED)
            return "guia_nao_obtida"
        return _settle_success(call, cycle_id, actor)


def _settle_success(
    call: ProviderCall, cycle_id: UUID | None, actor: User | None
) -> str:
    document = _guide_document(call)
    requested = _requested(call)
    if cycle_id is None and requested is not None:
        cycle_id = requested[0]
    if actor is None and requested is not None:
        from apps.accounts.models import User as Account  # noqa: PLC0415

        actor = Account.objects.filter(pk=requested[1]).first()
    if document is None or cycle_id is None or actor is None:
        _own(call, GUIDE_NOT_LINKED)
        return "guia_nao_vinculada"
    try:
        with transaction.atomic():
            cycle, link = _link_in_cycle(cycle_id, document, actor, call)
            _observe(call, cycle, document, link)
    except (
        IllegalTransition,
        VersionConflict,
        ValidationError,
        Http404,
        PermissionDenied,
        IntegrityError,
        ServiceCycle.DoesNotExist,
    ):
        _own(call, GUIDE_NOT_LINKED)
        return "guia_nao_vinculada"
    return "guia_obtida"


def evaluate_guide_conditions(tenant: Tenant, today: date) -> None:
    """Daily: settle every guide emission a dead worker left unsettled.

    No time window and no flag gate: an emission already sent (and possibly
    billed) is linked or owned whenever the sweep next runs, even after a week
    with `federal_checks` off. The flag gates new emissions only. Non-final calls
    are taken only after their lease or reservation handoff has run out.
    """
    candidates = ProviderCall.objects.filter(
        tenant_id=tenant.pk, operation=OPERATION
    ).order_by("pk")
    for call in candidates:
        if not CALL_FINAL[call.state] and not _lease_expired(call):
            continue
        if _unsettled(call):
            settle_guide(tenant.pk, call.pk)
    del today


def guide_panel(
    cycle: ServiceCycle,
    *,
    allowed: frozenset[str],
    profile: TaxProfile | None,
    requests: Sequence[EvidenceRequest],
    links: Sequence[EvidenceLink],
) -> dict[str, object] | None:
    """Context for the cycle page's guide section, from reads the page already holds.

    No query of its own: the regime comes from the page's coverage read (None when
    the engagement does not cover the competence). The service re-checks every
    gate, including the SERPRO link and the termo, when the action runs.
    """
    if cycle.engagement.service_kind != "mei_mensal" or not tenant_has(
        cycle.tenant_id, "federal_checks"
    ):
        return None
    reason = ""
    if CAPABILITY not in allowed:
        reason = "sem_permissao"
    elif cycle.state not in OPEN_STATES:
        reason = "ciclo_encerrado"
    elif profile is None or profile.regime != "mei":
        reason = "nao_mei"
    guide_requests = {
        row.pk
        for row in requests
        if row.title == GUIDE_TITLE and row.state == FIRM_ONLY_STATE
    }
    obtained = any(link.request_id in guide_requests for link in links)
    try:
        cost = brl(estimated_cost_microbrl())
    except GuideRefused:
        cost = ""
    return {
        "eligible": not reason,
        "reason": REFUSAL_LABELS.get(reason, ""),
        "code": "guia_obtida" if obtained else "",
        "status": OUTCOME_LABELS["guia_obtida"] if obtained else "",
        "cost": cost,
    }
