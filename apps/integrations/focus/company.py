"""Focus company status sync and the operator's audited setup and disable (A6 FC-EMP).

Every call is a journaled, included-plan configuration `ProviderCall` run through
the phased protocol (A7.4); each operator invocation is its own business-effect
occurrence, so a retry after a failed or uncertain call is a new, separately
journaled call rather than a repeat hidden behind the first one's barrier.

`sync_company_status(link)` reads the company and, inside the transaction that
finalizes the read, stores what Focus reports through AXRM-008's
`record_certificate_status` (A7.3 credential machine and its openers) plus an
allowlisted switch snapshot. A certificate Axorim did not expect -- another
CNPJ, a company of another CNPJ, an unrequested new validity window or a
certificate that disappeared -- is `divergente` and blocks capture dispatch.

Setup refuses before any write or request: unknown streams, malformed months, a
start before the pilot month or different from the link's immutable start month
(INV-R11), a link being disabled, a missing credential, and received NF-e without
the PQ-FOCUS-1 evidence id. A setup or disable is confirmed only by a read-back
GET that shows every written switch with the written value.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.audit.services import ObjectRef, record_event
from apps.core.tasks import PlatformTask, assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.integrations.allowlist import PQ_FOCUS_1_ID
from apps.integrations.authority import record_certificate_status
from apps.integrations.call_contracts import CallRequest, StaleClaimError
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_protocol import claim, perform, reserve
from apps.integrations.focus.adapter import (
    CompanyReport,
    FocusAdapter,
    company_request,
    limiter_key,
    request_digest,
    token_for,
    update_request,
)
from apps.integrations.models import (
    CaptureStream,
    ClientProviderLink,
    ConnectionStatus,
    CredentialState,
    DisablementState,
    LinkStatus,
    Provider,
)
from apps.integrations.services import (
    LINK_TRANSITIONS,
    LinkMachine,
    set_capture_start_month,
    transition_link,
)
from apps.integrations.transport import ProviderTransport

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.db.models import QuerySet

    from apps.integrations.transport_types import Environment, ProviderRequest

OPERATION: Final = "FC-EMP"
OPERATION_REVISION: Final = "v1"
NFE_QUALIFICATION_REFUSAL: Final = (
    "Captura de NF-e recebidas bloqueada: falta a evidencia PQ-FOCUS-1"
)
# The same compiled pattern the wire enforces (apps.integrations.allowlist).
QUALIFICATION_ID: Final = PQ_FOCUS_1_ID
CAPTURE_ENVIRONMENTS: Final = ("fake", "homologacao", "producao")
# A7.3 disablement: the operator's disable walks the local steps, then the provider's.
DISABLE_PATH: Final = (
    DisablementState.ATIVA,
    DisablementState.PAUSA_SOLICITADA,
    DisablementState.PAUSADA_LOCALMENTE,
    DisablementState.DESATIVACAO_NO_PROVEDOR_PENDENTE,
)
_MONTH: Final = re.compile(r"[0-9]{4}-[0-9]{2}")
_MISSING: Final = object()


class FocusRefusedError(Exception):
    """An operator request refused before any write or provider request."""


@dataclass(frozen=True, slots=True)
class CallOutcome:
    """One finished (or refused-in-flight) FC-EMP call, by its minimized result."""

    call_id: UUID
    state: str
    provider_code: str
    report: CompanyReport | None = None


# --- Shared call path ------------------------------------------------------------


def _transport(environment: Environment) -> ProviderTransport:
    if environment == "fake":
        return ProviderTransport(
            "focus", "fake", str(getattr(settings, "FOCUS_FAKE_ORIGIN", ""))
        )
    return ProviderTransport("focus", environment)


def _environment(link: ClientProviderLink) -> Environment:
    match link.connection.environment:
        case "fake":
            return "fake"
        case "homologacao":
            return "homologacao"
        case "producao":
            return "producao"
    message = "Ambiente Focus desconhecido para este vínculo."
    raise FocusRefusedError(message)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _call(
    link: ClientProviderLink,
    request: ProviderRequest,
    *,
    action: str,
    occurrence: str,
    keep: Callable[[FocusAdapter], Callable[[ProviderCall], None]] | None = None,
) -> CallOutcome:
    """Reserve, claim and perform one FC-EMP call; admission refusals propagate."""
    assert_no_transaction()
    environment = _environment(link)
    digest = request_digest(request)
    call_request = CallRequest(
        OPERATION,
        _sha(f"focus:{link.pk}:{OPERATION}:{OPERATION_REVISION}:{digest}:{occurrence}"),
        digest,
        _sha(
            f"{link.tenant_id}:{link.client_id}:{environment}:{action}:"
            f"{link.provider_company_ref}:{occurrence}"
        ),
    )
    call = reserve(link.tenant_id, link.pk, call_request)
    ticket = claim(link.tenant_id, call.pk)
    if ticket is not None:
        adapter = FocusAdapter(
            _transport(environment),
            request,
            token=token_for(link.connection),
            limiter_key=limiter_key(link.connection),
        )
        try:
            perform(ticket, adapter, keep=keep(adapter) if keep else None)
        except StaleClaimError:
            adapter.report = None
        report = adapter.report
    else:
        report = None
    with tenant_context(link.tenant_id):
        final = ProviderCall.objects.get(pk=call.pk)
    return CallOutcome(
        final.pk,
        final.state,
        final.provider_code,
        report if final.state == CallState.SUCCESS else None,
    )


# --- Status sync -----------------------------------------------------------------


def _apply_report(link_id: UUID, report: CompanyReport, today: dt.date | None) -> None:
    """Store one attributable report; runs inside the read's finalizing transaction."""
    link = (
        ClientProviderLink.objects.select_for_update(of=("self",))
        .select_related("client")
        .get(pk=link_id)
    )
    # A company registered for another CNPJ cannot vouch for any certificate.
    reported = report.reported_cnpj if report.company_cnpj == link.client.cnpj else ""
    if report.valid_to is not None:
        known = (
            link.certificate_cnpj,
            link.certificate_valid_from,
            link.certificate_valid_to,
        )
        new = (reported, report.valid_from, report.valid_to) != known
        link = record_certificate_status(
            link.pk,
            expected_version=link.version,
            certificate_cnpj=reported,
            valid_from=report.valid_from,
            valid_to=report.valid_to,
            credential_generation=link.credential_generation + int(new),
            today=today,
        )
    elif link.certificate_valid_to is not None:
        # A certificate Axorim recorded is no longer reported: provider-side drift.
        link = record_certificate_status(
            link.pk,
            expected_version=link.version,
            certificate_cnpj="",
            valid_from=link.certificate_valid_from,
            valid_to=link.certificate_valid_to,
            credential_generation=link.credential_generation,
            today=today,
        )
    else:
        link.certificate_synced_at = timezone.now()
        link.version += 1
    link.provider_flags = report.flags
    link.updated_at = timezone.now()
    link.save()
    record_event(
        action="integrations.focus_company_status_synced",
        tenant_id=link.tenant_id,
        obj=ObjectRef("ClientProviderLink", str(link.pk)),
        metadata={
            "credential_state": link.credential_state,
            "credential_generation": link.credential_generation,
            "version": link.version,
        },
    )


@dataclass(frozen=True, slots=True)
class StatusOutcome:
    """The read's call outcome and, on success, the stored report."""

    call_id: UUID
    state: str
    provider_code: str
    report: CompanyReport | None


def sync_company_status(
    link: ClientProviderLink,
    *,
    today: dt.date | None = None,
    occurrence: str | None = None,
) -> StatusOutcome:
    """Read the Focus company and store certificate status and switches (todo 19).

    Raises the protocol's `AdmissionDeniedError` when pauses, flags, environment,
    the link's state or the budget refuse the read; nothing is sent then.
    """

    def keep(adapter: FocusAdapter) -> Callable[[ProviderCall], None]:
        def store(_call: ProviderCall) -> None:
            # A successful read always carries its parsed report (adapter.send).
            if adapter.report is not None:
                _apply_report(link.pk, adapter.report, today)

        return store

    with tenant_context(link.tenant_id):
        link = ClientProviderLink.objects.select_related("connection").get(pk=link.pk)
    outcome = _call(
        link,
        company_request(link.provider_company_ref),
        action="focus_company_status",
        occurrence=occurrence or uuid4().hex,
        keep=keep,
    )
    return StatusOutcome(
        outcome.call_id, outcome.state, outcome.provider_code, outcome.report
    )


def capture_links() -> QuerySet[ClientProviderLink]:
    """Select the links the capture sweep (todo 21) may read, in the caller's tenant.

    Every A7.3 condition at once: an active Focus connection in the configured
    environment, an active link that is not being paused or disabled, a usable
    certificate state, at least one stream and an approved start month.
    """
    return (
        ClientProviderLink.objects.filter(
            connection__provider=Provider.FOCUS,
            connection__status=ConnectionStatus.ATIVA,
            connection__environment=settings.FOCUS_ENVIRONMENT,
            status=LinkStatus.ATIVA,
            disablement_state=DisablementState.ATIVA,
            credential_state__in=(CredentialState.ATIVA, CredentialState.VENCENDO),
            capture_start_month__isnull=False,
        )
        .exclude(streams=[])
        .order_by("pk")
    )


# --- Planning (no writes, no requests) ----------------------------------------------


def parse_streams(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated, duplicate-free subset of the A7.1 capture streams."""
    names = [name.strip() for name in raw.split(",")]
    # An empty name ("" or "a,,b") is never a known stream, so the subset check
    # refuses it too.
    if len(set(names)) != len(names) or set(names) - set(CaptureStream.values):
        message = "Fluxos inválidos: use nfe_recebidas e/ou nfsen_recebidas."
        raise FocusRefusedError(message)
    return tuple(sorted(names))


def parse_month(raw: str) -> dt.date:
    """Parse `AAAA-MM` as the first day of that month."""
    message = f"Mês inválido: {raw!r}; use AAAA-MM."
    if not _MONTH.fullmatch(raw):
        raise FocusRefusedError(message)
    try:
        return dt.date(int(raw[:4]), int(raw[5:]), 1)
    except ValueError:
        raise FocusRefusedError(message) from None


def pilot_month() -> dt.date:
    """Return the pilot start month; absent or malformed refuses every setup."""
    raw = str(getattr(settings, "FOCUS_CAPTURE_PILOT_START_MONTH", ""))
    try:
        return parse_month(raw)
    except FocusRefusedError:
        message = (
            "O mês de início do piloto (FOCUS_CAPTURE_PILOT_START_MONTH) não está "
            "configurado; nenhuma captura é configurada sem ele."
        )
        raise FocusRefusedError(message) from None


def _key_environment(environment: str) -> str:
    """Focus switches exist for production and homologation; fake mirrors the latter."""
    return "producao" if environment == "producao" else "homologacao"


def _nfe_key(environment: str) -> str:
    return (
        "habilita_manifestacao"
        if environment == "producao"
        else "habilita_manifestacao_homologacao"
    )


def setup_fields(
    streams: list[str] | tuple[str, ...], start: dt.date, environment: str
) -> dict[str, bool | str]:
    """Build the exact FC-EMP fields for these streams, start and environment."""
    fields: dict[str, bool | str] = {
        "habilita_filtro_mde_destinatario_empresa": True,
        "certificado_especifico": True,
    }
    if CaptureStream.NFSEN_RECEBIDAS in streams:
        fields[f"habilita_nfsen_recebidas_{_key_environment(environment)}"] = True
        fields["data_inicio_recebimento_nfsen"] = start.isoformat()
    if CaptureStream.NFE_RECEBIDAS in streams:
        fields[_nfe_key(environment)] = True
        fields["data_inicio_recebimento_nfe"] = start.isoformat()
    return dict(sorted(fields.items()))


def disable_fields(
    streams: list[str] | tuple[str, ...], environment: str
) -> dict[str, bool | str]:
    """Turn these streams' receipt switches off; start dates never change."""
    fields: dict[str, bool | str] = {}
    if CaptureStream.NFSEN_RECEBIDAS in streams:
        fields[f"habilita_nfsen_recebidas_{_key_environment(environment)}"] = False
    if CaptureStream.NFE_RECEBIDAS in streams:
        fields[_nfe_key(environment)] = False
    return dict(sorted(fields.items()))


def locate_link(client_id: UUID) -> ClientProviderLink:
    """Find the client's Focus link in the configured environment and tenant."""
    for tenant_id in PlatformTask.tenant_ids():
        with tenant_context(tenant_id):
            link = (
                ClientProviderLink.objects.select_related("connection", "client")
                .filter(
                    client_id=client_id,
                    connection__provider=Provider.FOCUS,
                    connection__environment=settings.FOCUS_ENVIRONMENT,
                )
                .first()
            )
        if link is not None:
            return link
    message = "Cliente sem vínculo Focus no ambiente configurado."
    raise FocusRefusedError(message)


def _require_credential(link: ClientProviderLink) -> None:
    if token_for(link.connection) is None:
        message = (
            "Sem credencial Focus configurada para esta conexão "
            f"(segredo {link.connection.secret_ref or 'não informado'})."
        )
        raise FocusRefusedError(message)


@dataclass(frozen=True, slots=True)
class SetupPlan:
    """A validated setup: who, where and the exact fields, before anything runs."""

    tenant_id: UUID
    client_id: UUID
    link_id: UUID
    environment: str
    streams: tuple[str, ...]
    start_month: dt.date
    fields: dict[str, bool | str]
    qualification_ref: str = ""


def plan_setup(client_id: UUID, streams: str, start_month: str) -> SetupPlan:
    """Validate an operator setup; every refusal precedes every write and request."""
    chosen = parse_streams(streams)
    start = parse_month(start_month)
    pilot = pilot_month()
    if start < pilot:
        message = (
            f"O início {start:%Y-%m} é anterior ao início do piloto ({pilot:%Y-%m}); "
            "histórico exige decisão do responsável."
        )
        raise FocusRefusedError(message)
    link = locate_link(client_id)
    environment = _environment(link)
    if link.status == LinkStatus.REVOGADA:
        message = "Vínculo revogado: a captura não pode ser configurada."
        raise FocusRefusedError(message)
    if link.disablement_state != DisablementState.ATIVA:
        message = (
            f"Vínculo em desativação ({link.disablement_state}): a captura não é "
            "reconfigurada por este comando."
        )
        raise FocusRefusedError(message)
    if link.capture_start_month is not None and link.capture_start_month != start:
        message = (
            "O mês de início da captura já foi definido "
            f"({link.capture_start_month:%Y-%m}) e não muda."
        )
        raise FocusRefusedError(message)
    reference = str(getattr(settings, "FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF", ""))
    if CaptureStream.NFE_RECEBIDAS in chosen and not QUALIFICATION_ID.fullmatch(
        reference
    ):
        raise FocusRefusedError(NFE_QUALIFICATION_REFUSAL)
    _require_credential(link)
    return SetupPlan(
        link.tenant_id,
        link.client_id,
        link.pk,
        environment,
        chosen,
        start,
        setup_fields(chosen, start, environment),
        reference if CaptureStream.NFE_RECEBIDAS in chosen else "",
    )


def _load(tenant_id: UUID, link_id: UUID) -> ClientProviderLink:
    with tenant_context(tenant_id):
        return ClientProviderLink.objects.select_related("connection", "client").get(
            pk=link_id
        )


def _unconfirmed(
    fields: dict[str, bool | str], report: CompanyReport | None
) -> list[str]:
    """Written fields the read-back does not show with the written value."""
    if report is None:
        return list(fields)
    # Flags hold only booleans, None and ISO days (`adapter._flag` refuses the
    # rest), so equality is exact; an absent switch is never a confirmation.
    return [
        name
        for name, value in fields.items()
        if report.flags.get(name, _MISSING) != value
    ]


@dataclass(frozen=True, slots=True)
class ConfigurationOutcome:
    """A setup or disable: the write, its read-back and what is still unconfirmed."""

    write: CallOutcome | None
    readback: StatusOutcome | None
    unconfirmed: list[str] = field(default_factory=list)
    link_status: str = ""
    disablement_state: str = ""
    credential_state: str = ""

    @property
    def confirmed(self) -> bool:
        """Every written switch read back with its written value.

        A read-back exists only after a successful write (`run_setup`,
        `run_disable`), so it stands for the write's success too.
        """
        return self.readback is not None and not self.unconfirmed

    @property
    def call_ids(self) -> list[str]:
        """The journaled calls of this operator action."""
        calls = (self.write, self.readback)
        return [str(item.call_id) for item in calls if item is not None]


def _event(link: ClientProviderLink, action: str, **metadata: object) -> None:
    record_event(
        action=action,
        tenant_id=link.tenant_id,
        obj=ObjectRef("ClientProviderLink", str(link.pk)),
        metadata=metadata,
    )


def _confirm_setup(plan: SetupPlan) -> ClientProviderLink:
    with tenant_context(plan.tenant_id), transaction.atomic():
        link = ClientProviderLink.objects.select_for_update().get(pk=plan.link_id)
        # Pending -> active; an active link stays; a link revoked meanwhile is
        # never revived (the machine allows no move out of `revogada`).
        allowed = LINK_TRANSITIONS[LinkMachine.STATUS].get(link.status, frozenset())
        if LinkStatus.ATIVA in allowed:
            link.status = LinkStatus.ATIVA
        link.streams = sorted({*link.streams, *plan.streams})
        link.version += 1
        link.updated_at = timezone.now()
        link.save()
        _event(
            link,
            "integrations.focus_setup_confirmed",
            streams=link.streams,
            status=link.status,
            version=link.version,
        )
        return link


def run_setup(plan: SetupPlan, *, dry_run: bool = False) -> ConfigurationOutcome:
    """Write the planned fields (or let Focus validate them), then read them back.

    The start month is fenced locally before the write: Focus keeps
    `data_inicio_recebimento_*` immutable once set, so an uncertain write must never
    be followed by a different month.
    """
    occurrence = uuid4().hex
    link = _load(plan.tenant_id, plan.link_id)
    request = update_request(link.provider_company_ref, plan.fields, dry_run=dry_run)
    if dry_run:
        write = _call(
            link, request, action="focus_company_setup_dry_run", occurrence=occurrence
        )
        return ConfigurationOutcome(
            write,
            None,
            [] if write.state == CallState.SUCCESS else list(plan.fields),
            link.status,
            link.disablement_state,
            link.credential_state,
        )
    if link.capture_start_month is None:
        set_capture_start_month(
            plan.tenant_id,
            link.pk,
            month=plan.start_month,
            expected_version=link.version,
        )
        link = _load(plan.tenant_id, plan.link_id)
    with tenant_context(plan.tenant_id):
        _event(
            link,
            "integrations.focus_setup_requested",
            fields=sorted(plan.fields),
            streams=list(plan.streams),
            start_month=plan.start_month.isoformat(),
            qualification_ref=plan.qualification_ref,
            occurrence=occurrence,
        )
    write = _call(link, request, action="focus_company_setup", occurrence=occurrence)
    readback = None
    if write.state == CallState.SUCCESS:
        readback = sync_company_status(link, occurrence=occurrence)
    unconfirmed = _unconfirmed(plan.fields, readback.report if readback else None)
    outcome = ConfigurationOutcome(write, readback, unconfirmed)
    if outcome.confirmed:
        link = _confirm_setup(plan)
    else:
        link = _load(plan.tenant_id, plan.link_id)
        with tenant_context(plan.tenant_id):
            _event(
                link,
                "integrations.focus_setup_unconfirmed",
                write_state=write.state,
                unconfirmed=unconfirmed,
            )
    return ConfigurationOutcome(
        write,
        readback,
        unconfirmed,
        link.status,
        link.disablement_state,
        link.credential_state,
    )


# --- Disable ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DisablePlan:
    """Which receipt switches go off, and whether that stops the whole link."""

    tenant_id: UUID
    link_id: UUID
    environment: str
    streams: tuple[str, ...]
    remaining: tuple[str, ...]
    fields: dict[str, bool | str]

    @property
    def whole_link(self) -> bool:
        """No configured stream remains: the link itself is disabled (A7.3)."""
        return not self.remaining


def plan_disable(client_id: UUID, streams: list[str] | None = None) -> DisablePlan:
    """Default to every configured stream (or every stream when none is recorded)."""
    link = locate_link(client_id)
    environment = _environment(link)
    if streams:
        chosen = parse_streams(",".join(streams))
    else:
        chosen = tuple(sorted(link.streams)) or tuple(sorted(CaptureStream.values))
    _require_credential(link)
    remaining = tuple(name for name in sorted(link.streams) if name not in chosen)
    return DisablePlan(
        link.tenant_id,
        link.pk,
        environment,
        chosen,
        remaining,
        disable_fields(chosen, environment),
    )


def _stop_locally(plan: DisablePlan, link: ClientProviderLink) -> ClientProviderLink:
    """Stop capture before the provider write: walk A7.3, or drop the streams."""
    if plan.whole_link:
        index = DISABLE_PATH.index(DisablementState(link.disablement_state))
        for target in DISABLE_PATH[index + 1 :]:
            link = transition_link(
                plan.tenant_id,
                link.pk,
                machine=LinkMachine.DISABLEMENT,
                to=target,
                expected_version=link.version,
            )
        return _load(plan.tenant_id, plan.link_id)
    with tenant_context(plan.tenant_id), transaction.atomic():
        locked = ClientProviderLink.objects.select_for_update().get(pk=plan.link_id)
        locked.streams = list(plan.remaining)
        locked.version += 1
        locked.updated_at = timezone.now()
        locked.save()
        _event(
            locked,
            "integrations.focus_streams_stopped",
            streams=list(plan.streams),
            remaining=list(plan.remaining),
            version=locked.version,
        )
    return _load(plan.tenant_id, plan.link_id)


def run_disable(plan: DisablePlan) -> ConfigurationOutcome:
    """Stop locally, turn the receipt switches off at Focus, confirm by read-back."""
    occurrence = uuid4().hex
    link = _load(plan.tenant_id, plan.link_id)
    confirmed_state = DisablementState.DESATIVADA_NO_PROVEDOR_CONFIRMADA
    if plan.whole_link and link.disablement_state == confirmed_state:
        return ConfigurationOutcome(
            None,
            None,
            [],
            link.status,
            link.disablement_state,
            link.credential_state,
        )
    link = _stop_locally(plan, link)
    with tenant_context(plan.tenant_id):
        _event(
            link,
            "integrations.focus_disable_requested",
            fields=sorted(plan.fields),
            streams=list(plan.streams),
            occurrence=occurrence,
        )
    request = update_request(link.provider_company_ref, plan.fields)
    write = _call(link, request, action="focus_company_disable", occurrence=occurrence)
    readback = None
    if write.state == CallState.SUCCESS:
        readback = sync_company_status(link, occurrence=occurrence)
    unconfirmed = _unconfirmed(plan.fields, readback.report if readback else None)
    if ConfigurationOutcome(write, readback, unconfirmed).confirmed and plan.whole_link:
        current = _load(plan.tenant_id, plan.link_id)
        transition_link(
            plan.tenant_id,
            current.pk,
            machine=LinkMachine.DISABLEMENT,
            to=confirmed_state,
            expected_version=current.version,
        )
    link = _load(plan.tenant_id, plan.link_id)
    return ConfigurationOutcome(
        write,
        readback,
        unconfirmed,
        link.status,
        link.disablement_state,
        link.credential_state,
    )
