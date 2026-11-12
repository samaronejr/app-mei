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
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from django.conf import settings
from django.db import (
    DEFAULT_DB_ALIAS,
    DatabaseError,
    OperationalError,
    connections,
    transaction,
)
from django.utils import timezone

from apps.audit.services import ObjectRef, record_event
from apps.core.tasks import PlatformTask, assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.integrations.allowlist import PQ_FOCUS_1_ID
from apps.integrations.authority import record_certificate_status
from apps.integrations.budget_alerts import _own_issue
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
    IssueReason,
    LinkStatus,
    Provider,
)
from apps.integrations.services import (
    LINK_TRANSITIONS,
    LinkMachine,
    transition_link,
)
from apps.integrations.transport import ProviderTransport

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from django.db.backends.base.base import BaseDatabaseWrapper
    from django.db.backends.utils import CursorWrapper
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


class LinkBusyError(FocusRefusedError):
    """The link's exchange lock was not granted within LINK_LOCK_TIMEOUT."""


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


def _surface_reopened_receipt(link: ClientProviderLink, report: CompanyReport) -> None:
    """Own the mismatch when a confirmed-disabled link reads a receipt switch on.

    A crash between a setup's write and its compensation can leave a switch on at
    Focus after a confirmed disable. The sweep already skips the link; this makes the
    mismatch visible (an owned issue and an audit event) so the operator re-runs
    focus_company_disable. Nothing is sent from here; active links are untouched.
    """
    if link.disablement_state != DisablementState.DESATIVADA_NO_PROVEDOR_CONFIRMADA:
        return
    managed = disable_fields(tuple(sorted(CaptureStream.values)), _environment(link))
    reopened = sorted(name for name in managed if report.flags.get(name) is True)
    if not reopened:
        return
    _own_issue(link, IssueReason.RECEBIMENTO_REABERTO_PROVEDOR, link.pk, "")
    record_event(
        action="integrations.focus_receipt_reopened",
        tenant_id=link.tenant_id,
        obj=ObjectRef("ClientProviderLink", str(link.pk)),
        metadata={"switches": reopened, "needs_redisable": True},
    )


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
    _surface_reopened_receipt(link, report)
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
    sent: dict[str, bool | str] = field(default_factory=dict)
    streams: tuple[str, ...] = ()

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
        # A disable since the fence may have removed a stream this setup wrote; the
        # union never restores it (RV5). Without a moved epoch every stream is added.
        epoch = _FENCE_EPOCH.get()
        moved = epoch is not None and link.disable_epoch != epoch
        withheld = sorted(s for s in plan.streams if moved and s not in link.streams)
        link.streams = sorted(
            {*link.streams, *(s for s in plan.streams if s not in withheld)}
        )
        if withheld:
            _event(
                link,
                "integrations.focus_setup_streams_withheld",
                streams=withheld,
                disable_epoch=link.disable_epoch,
            )
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


# Setup and disable of one link never interleave (RV2): bounded wait, then refuse.
LINK_LOCK_TIMEOUT: Final = "30s"
LOCK_NOT_AVAILABLE: Final = "55P03"
_LOCKED_LINK: ContextVar[UUID | None] = ContextVar("focus_link_lock", default=None)
# The disable epoch a running setup saw at its fence (RV5 confirmation).
_FENCE_EPOCH: ContextVar[int | None] = ContextVar("focus_fence_epoch", default=None)
_LOCK_CONNECTION: ContextVar[BaseDatabaseWrapper | None] = ContextVar(
    "focus_link_lock_connection", default=None
)
# The lock connection's own backend holds the link's key (bigint advisory form).
_HOLDS_LOCK_SQL: Final = """
    WITH k AS (SELECT hashtext(%s)::bigint AS v)
    SELECT EXISTS (
        SELECT 1 FROM pg_locks l, k
        WHERE l.locktype = 'advisory' AND l.pid = pg_backend_pid() AND l.granted
          AND l.objsubid = 1
          AND l.classid::bigint = ((k.v >> 32) & 4294967295)
          AND l.objid::bigint = (k.v & 4294967295)
    )
"""


def _lock_key(link_id: UUID) -> str:
    return f"focus-link:{link_id}"


def _wait_for_link_lock(cursor: CursorWrapper, key: str) -> None:
    cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [key])


@contextmanager
def link_exchange_lock(link_id: UUID) -> Iterator[None]:
    """Serialize setup and disable of one link across the whole provider exchange.

    A transaction-level advisory lock keyed on the link is taken on a dedicated
    connection and held through the fence, the PUT and the read-back, so a disable
    can never be overtaken by a setup's late write. The main connection stays outside
    any transaction, so journal rows still commit before each send. The wait is
    bounded by LINK_LOCK_TIMEOUT; a timeout refuses with nothing sent. A crash drops
    the connection and so the lock. One lock at a time: no provider call ever runs
    while another link's lock is held.
    """
    if _LOCKED_LINK.get() is not None:
        message = "a Focus link exchange lock is already held by this context"
        raise RuntimeError(message)
    lock = connections.create_connection(DEFAULT_DB_ALIAS)
    try:
        lock.ensure_connection()
        lock.set_autocommit(False)
        try:
            with lock.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('lock_timeout', %s, true)", [LINK_LOCK_TIMEOUT]
                )
                _wait_for_link_lock(cursor, _lock_key(link_id))
        except OperationalError as error:
            if getattr(error.__cause__, "sqlstate", None) != LOCK_NOT_AVAILABLE:
                raise
            message = (
                "Outra operação Focus neste vínculo está em andamento; nada foi "
                "enviado. Tente novamente."
            )
            raise LinkBusyError(message) from None
        token = _LOCKED_LINK.set(link_id)
        held = _LOCK_CONNECTION.set(lock)
        try:
            yield
        finally:
            _LOCK_CONNECTION.reset(held)
            _LOCKED_LINK.reset(token)
    finally:
        lock.close()


def _require_link_lock(link_id: UUID) -> None:
    """Refuse unless this context still holds the link's lock, right before a send.

    The lock lives on a dedicated connection; if that connection was lost (backend
    terminated, network, server restart) the lock is gone and another operation on
    the link may already run. Any failure to prove the lock is a refusal, unsent.
    """
    lock = _LOCK_CONNECTION.get()
    held = False
    if lock is not None and _LOCKED_LINK.get() == link_id:
        try:
            with lock.cursor() as cursor:
                cursor.execute(_HOLDS_LOCK_SQL, [_lock_key(link_id)])
                row = cursor.fetchone()
                held = bool(row and row[0])
        except DatabaseError:
            held = False
    if not held:
        message = (
            "O bloqueio deste vínculo foi perdido antes do envio; nada foi enviado. "
            "Tente novamente."
        )
        raise FocusRefusedError(message)


def _reread_locked(plan: SetupPlan) -> ClientProviderLink:
    """Read the link as stored now, under its row lock (no provider I/O inside)."""
    with tenant_context(plan.tenant_id), transaction.atomic():
        return ClientProviderLink.objects.select_for_update().get(pk=plan.link_id)


@contextmanager
def _relocked(link_id: UUID) -> Iterator[None]:
    """Hold the link's exchange lock for a compensation, re-acquiring it if lost.

    A setup compensates because its lock may have been lost; a newer setup or disable
    may then have run. If this context still proves its lock it keeps it; otherwise
    it takes a fresh lock (bounded wait, LinkBusyError on timeout) so the
    compensation serializes with any newer operation on the link.
    """
    try:
        _require_link_lock(link_id)
    except FocusRefusedError:
        held = False
    else:
        held = True
    if held:
        yield
        return
    locked = _LOCKED_LINK.set(None)
    connection = _LOCK_CONNECTION.set(None)
    try:
        with link_exchange_lock(link_id):
            yield
    finally:
        _LOCK_CONNECTION.reset(connection)
        _LOCKED_LINK.reset(locked)


def _defer_undo(plan: SetupPlan, occurrence: str, reason: str) -> None:
    """Own a compensation that could not run: an issue and an event, nothing sent."""
    with tenant_context(plan.tenant_id), transaction.atomic():
        link = ClientProviderLink.objects.select_for_update().get(pk=plan.link_id)
        _own_issue(link, IssueReason.RECEBIMENTO_REABERTO_PROVEDOR, link.pk, "")
        _event(
            link,
            "integrations.focus_setup_compensation_deferred",
            reason=reason,
            setup_occurrence=occurrence,
        )


def _undo_setup(plan: SetupPlan, link: ClientProviderLink, occurrence: str) -> None:
    """Switch receipt off again when a disable ran while this setup's write was out.

    The fence saw an active link, but a disable ran since (for example the setup's
    lock connection was lost mid-exchange). The compensation runs under the link's
    exchange lock (`_relocked`) and decides from the link as stored now, re-read
    under its row lock: every managed switch for a link no longer active, otherwise
    only the streams this setup wrote that the link does not hold now, so a newer
    setup that restored a stream is never undone (RV5). With nothing left to undo it
    sends nothing. Either way the setup reports a refusal. If the lock cannot be
    had (or is lost again before the write), nothing is sent: an issue and an event
    record the pending re-disable. `link` is the setup's earlier view, kept for the
    interface; the decision never uses it.
    """
    del link
    try:
        with _relocked(plan.link_id):
            _compensate(plan, occurrence)
    except LinkBusyError:
        _defer_undo(plan, occurrence, "lock_not_granted")
        message = (
            "Uma desativação ocorreu durante esta configuração e o vínculo está "
            "ocupado: nada foi enviado; uma pendência foi aberta para rodar "
            "focus_company_disable. A configuração não foi aplicada."
        )
        raise FocusRefusedError(message) from None


def _compensate(plan: SetupPlan, occurrence: str) -> None:
    """Under the exchange lock: undo from fresh state, confirm, and refuse."""
    fresh = _reread_locked(plan)
    state = fresh.disablement_state
    if state != DisablementState.ATIVA:
        streams = tuple(sorted(CaptureStream.values))
    else:
        streams = tuple(name for name in plan.streams if name not in fresh.streams)
    if not streams:
        with tenant_context(plan.tenant_id):
            _event(
                fresh,
                "integrations.focus_setup_compensation_skipped",
                setup_occurrence=occurrence,
            )
        message = (
            "Uma desativação ocorreu durante esta configuração; nada a desfazer: o "
            "vínculo já tem os fluxos desta configuração por uma operação mais "
            "recente. A configuração não foi aplicada."
        )
        raise FocusRefusedError(message)
    try:
        _require_link_lock(plan.link_id)
    except FocusRefusedError:
        _defer_undo(plan, occurrence, "lock_lost_before_write")
        message = (
            "Uma desativação ocorreu durante esta configuração e o bloqueio do "
            "vínculo foi perdido: nada foi enviado; uma pendência foi aberta para "
            "rodar focus_company_disable. A configuração não foi aplicada."
        )
        raise FocusRefusedError(message) from None
    # Reload through the tenant context: the locked row's relations (its connection)
    # are only visible there.
    link = _load(plan.tenant_id, plan.link_id)
    fields = disable_fields(streams, plan.environment)
    # A new journaled occurrence: the setup's own read-back has the same request
    # digest, so reusing its occurrence would replay that read instead of a new GET.
    undo = uuid4().hex
    with tenant_context(plan.tenant_id):
        _event(
            link,
            "integrations.focus_setup_compensated",
            fields=sorted(fields),
            disablement_state=state,
            occurrence=undo,
            setup_occurrence=occurrence,
        )
    request = update_request(link.provider_company_ref, fields)
    write = _call(link, request, action="focus_company_disable", occurrence=undo)
    readback = None
    if write.state == CallState.SUCCESS:
        readback = sync_company_status(link, occurrence=undo)
    unconfirmed = _unconfirmed(fields, readback.report if readback else None)
    confirmed = ConfigurationOutcome(write, readback, unconfirmed).confirmed
    result = "confirmado" if confirmed else "NÃO confirmado; rode focus_company_disable"
    message = (
        "Uma desativação ocorreu durante esta configuração; os recebimentos foram "
        f"desligados de novo na Focus ({result}). A configuração não foi aplicada."
    )
    raise FocusRefusedError(message)


def _fence_start_month(plan: SetupPlan, *, dry_run: bool) -> ClientProviderLink:
    """Check the plan's month against the stored link under its lock, before the write.

    A plan is computed earlier than it runs; another setup may have fenced a different
    month meanwhile. Under the link's row lock the stored month must be absent or equal
    to the plan's, or nothing is sent. A real setup fences an absent month in the same
    transaction. Once set, the month never changes (database trigger, INV-R11), so the
    write that follows can only carry the stored month.
    """
    with tenant_context(plan.tenant_id), transaction.atomic():
        link = ClientProviderLink.objects.select_for_update().get(pk=plan.link_id)
        if link.disablement_state != DisablementState.ATIVA:
            # A disable started or was confirmed after this plan was computed: a setup
            # must never re-enable capture at Focus (A7.3 recovery stays disabled).
            message = (
                f"Vínculo em desativação ({link.disablement_state}): a configuração "
                "não reativa a captura; nada foi enviado."
            )
            raise FocusRefusedError(message)
        stored = link.capture_start_month
        if stored is not None and stored != plan.start_month:
            message = (
                f"O mês de início da captura já foi definido ({stored:%Y-%m}) e não "
                f"muda; nada foi enviado com {plan.start_month:%Y-%m}."
            )
            raise FocusRefusedError(message)
        if stored is None and not dry_run:
            link.capture_start_month = plan.start_month
            link.version += 1
            link.updated_at = timezone.now()
            link.save(update_fields=["capture_start_month", "version", "updated_at"])
            record_event(
                action="integrations.capture_start_set",
                tenant_id=plan.tenant_id,
                obj=ObjectRef("ClientProviderLink", str(link.pk)),
                metadata={
                    "month": plan.start_month.isoformat(),
                    "version": link.version,
                },
            )
    return _load(plan.tenant_id, plan.link_id)


def run_setup(plan: SetupPlan, *, dry_run: bool = False) -> ConfigurationOutcome:
    """Run the setup exchange under the link's exchange lock (`link_exchange_lock`).

    A read-only fence check runs first, so a stale plan is refused at once instead of
    waiting for the lock; the authoritative check runs again inside the lock.
    """
    _fence_start_month(plan, dry_run=True)
    with link_exchange_lock(plan.link_id):
        return _run_setup(plan, dry_run=dry_run)


def _run_setup(plan: SetupPlan, *, dry_run: bool) -> ConfigurationOutcome:
    """Write the planned fields (or let Focus validate them), then read them back.

    The start month is fenced locally before the write: Focus keeps
    `data_inicio_recebimento_*` immutable once set, so an uncertain write must never
    be followed by a different month.
    """
    occurrence = uuid4().hex
    link = _fence_start_month(plan, dry_run=dry_run)
    # Read before the lock proof below: if that proof passes, no disable ran since.
    epoch = link.disable_epoch
    fenced = _FENCE_EPOCH.set(epoch)
    try:
        return _exchange_setup(plan, link, epoch, occurrence, dry_run=dry_run)
    finally:
        _FENCE_EPOCH.reset(fenced)


def _exchange_setup(
    plan: SetupPlan,
    link: ClientProviderLink,
    epoch: int,
    occurrence: str,
    *,
    dry_run: bool,
) -> ConfigurationOutcome:
    """Send the setup (or let Focus validate it), read it back and confirm it."""
    request = update_request(link.provider_company_ref, plan.fields, dry_run=dry_run)
    # The last check before any send (dry run or write): only an audit row follows.
    _require_link_lock(plan.link_id)
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
    current = _reread_locked(plan)
    if current.disablement_state != DisablementState.ATIVA or (
        current.disable_epoch != epoch
        and any(name not in current.streams for name in plan.streams)
    ):
        _undo_setup(plan, current, occurrence)
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
    scoped: bool = False

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
        scoped=bool(streams),
    )


def _bump_disable_epoch(plan: DisablePlan) -> ClientProviderLink:
    """Mark a whole-link disable on the link, under its row lock, before the walk.

    Every disable bumps the epoch so a setup that lost its lock can see it even when
    `disablement_state` stayed `ativa` (RV4). A stream-scoped disable bumps it in
    its own local stop (`_stop_locally`); a whole-link disable walks A7.3 through
    `transition_link`, so it bumps here first.
    """
    with tenant_context(plan.tenant_id), transaction.atomic():
        locked = ClientProviderLink.objects.select_for_update().get(pk=plan.link_id)
        locked.disable_epoch += 1
        locked.version += 1
        locked.updated_at = timezone.now()
        locked.save(update_fields=["disable_epoch", "version", "updated_at"])
    return _load(plan.tenant_id, plan.link_id)


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
        locked.disable_epoch += 1
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


def _decide_disable(plan: DisablePlan) -> DisablePlan:
    """Decide what to turn off from the link as stored now, inside the link lock.

    A plan is computed before the lock; a setup may have added a stream meanwhile
    (RV3). A whole-link disable turns off every receipt switch the adapter manages,
    configured locally or not, so no stream can stay on at Focus. A stream-scoped
    disable keeps to its streams, and what remains comes from the stored link.
    """
    stored = tuple(sorted(_load(plan.tenant_id, plan.link_id).streams))
    every = tuple(sorted(CaptureStream.values))
    if plan.scoped:
        remaining = tuple(name for name in stored if name not in plan.streams)
        fields = disable_fields(plan.streams, plan.environment)
        return replace(plan, remaining=remaining, fields=fields)
    chosen = stored or every
    return replace(
        plan,
        streams=chosen,
        remaining=(),
        fields=disable_fields(every, plan.environment),
    )


def run_disable(plan: DisablePlan) -> ConfigurationOutcome:
    """Run the disable exchange under the link's lock (`link_exchange_lock`)."""
    with link_exchange_lock(plan.link_id):
        return _run_disable(plan)


def _run_disable(plan: DisablePlan) -> ConfigurationOutcome:
    """Stop locally, turn the receipt switches off at Focus, confirm by read-back."""
    occurrence = uuid4().hex
    plan = _decide_disable(plan)
    link = _load(plan.tenant_id, plan.link_id)
    confirmed_state = DisablementState.DESATIVADA_NO_PROVEDOR_CONFIRMADA
    # The local state says capture already stopped; only Focus's read-back says the
    # switches are off. A repeated disable therefore always re-sends and re-confirms.
    already_confirmed = plan.whole_link and link.disablement_state == confirmed_state
    if plan.whole_link:
        link = _bump_disable_epoch(plan)
    if not already_confirmed:
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
    confirmed = ConfigurationOutcome(write, readback, unconfirmed).confirmed
    if confirmed and plan.whole_link and not already_confirmed:
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
        sent=dict(plan.fields),
        streams=plan.streams,
    )
