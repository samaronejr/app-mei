"""Consult federal payments page by page and request one receipt per payment.

AXRM-015 (plan todo 15; A6 SP-PAG/SP-COMP, A7.4, INV-R4/R5/R7). A `PaymentQuery`
is the consultation the firm sees; every page is one ordinary protocol call
(reserve -> claim -> perform, I/O outside any transaction) whose finalized result
becomes todo 14's per-call `FederalConsultation` receipt. Rules:

* Paging stops at the first page with fewer than 100 documents and never sends an
  eleventh page; ten full pages end as `falha` (`limite_de_paginas`), never as a
  complete answer.
* A budget refusal on the first page is `cota_esgotada`; on a later page the
  consultation is `falha` with the pages already fetched kept (their payments stay).
* A complete 403 records `ausente_detectada` (todo 8) and opens owned work; the
  protocol never retries it, and admission refuses the client's next call until the
  authority is declared and verified again.
* Nothing here infers non-payment: an empty answer is a `nada_localizado` receipt.
* The runner about to pay for a page takes the consultation's settlement lease
  first and renews it on every later page; while it is live (database time) any
  other runner yields without writing, and `integrations.0008` refuses such a
  write outright. Every runner also holds a session lock on its own database
  connection while it runs; only once the lease has run out AND that connection
  is gone (the owner died, `integrations.0009`) may a redelivered runner take over
  and resume, never re-fetching a page another runner paid for. A runner merely
  stalled past its lease keeps its consultation and the page it paid for.
* A page reserved but never claimed is cancelled and its reservation released as
  soon as the consultation is final, so no abandoned page holds the firm's cap.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import timedelta
from logging import getLogger
from typing import TYPE_CHECKING, Final, cast
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import (
    IntegrityError,
    InterfaceError,
    OperationalError,
    connection,
    transaction,
)
from django.db.models import BooleanField, Func, Q
from django.db.models.functions import Now
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.services import ObjectRef, record_event
from apps.authz.objects import load_visible_client
from apps.authz.services import can
from apps.clients.models import ClientCompany
from apps.core.flags import tenant_has
from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.cycles.intake import UploadRejected, UploadScannerOff
from apps.cycles.services.exceptions import ExceptionReason
from apps.integrations.authority import (
    Problem,
    authority_subject,
    raise_problem,
    record_authority_denial,
)
from apps.integrations.budget_pricing import BudgetDeniedError
from apps.integrations.budget_settlement import release
from apps.integrations.call_contracts import AdmissionDeniedError, CallRequest
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_protocol import claim, perform, reserve
from apps.integrations.models import (
    AuthorityService,
    ClientProviderLink,
    IssueReason,
    LinkStatus,
)
from apps.integrations.serpro.adapter import Parties, SerproAdapter, build_request
from apps.integrations.serpro.models import (
    ACTIVE_QUERY_STATES,
    PaymentQuery,
    PaymentQueryState,
)
from apps.integrations.serpro.pagtoweb import (
    MAX_PAGES,
    PAGE_OPERATION,
    PAGE_SIZE,
    RECEIPT_OPERATION,
    PageAdapter,
    PaymentRange,
    ReceiptAdapter,
    page_data,
)
from apps.integrations.serpro.parties import termo_parties
from apps.integrations.services import IntegrationConflictError
from apps.integrations.transport import ProviderTransport
from apps.obligations.federal import (
    ConsultationCall,
    FederalRecordConflictError,
    record_payment_consultation,
)
from apps.obligations.models import (
    AcquisitionOrigin,
    FederalConsultationOutcome,
    FederalPayment,
)
from apps.obligations.payment_matching import match_das_payments
from apps.obligations.system_ingest import ingest_system_document

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import date
    from uuid import UUID

    from django.db.models import QuerySet

    from apps.integrations.transport_types import Environment, JSONValue

logger = getLogger(__name__)

CHECKS: Final = "fiscal.checks.run"
FLAG: Final = "federal_checks"
# Covers one claim, a send under its 120 s call lease, the one fresh claim after a
# SERPRO 401 and the page's settlement, with margin. Past it, an owner whose
# database session is gone too is dead.
SETTLEMENT_LEASE: Final = timedelta(minutes=10)
OWNER_SETTING: Final = "app.payment_settlement_owner"
# Session-level advisory lock keyed by the runner's token; `integrations.0009`'s
# `integrations_payment_runner_alive` derives the same key from the stored owner.
RUNNER_LOCK: Final = "SELECT pg_advisory_lock(hashtextextended(%s, 0))"
RUNNER_UNLOCK: Final = "SELECT pg_advisory_unlock(hashtextextended(%s, 0))"
UNUSED_PAGE: Final = "consulta_encerrada"
FAILED_REASON: Final = IssueReason.CONSULTA_PAGAMENTOS_FALHOU
REASONS: Final = {
    FAILED_REASON: ExceptionReason(
        "Confira a consulta de pagamentos na Receita: ela terminou incompleta, com "
        "falha ou com resultado incerto. As páginas já obtidas foram mantidas; uma "
        "nova consulta é uma decisão da equipe.",
        label="consulta de pagamentos na Receita com falha",
    ),
}
_RECEIPT_OUTCOME: Final[dict[str, str]] = {
    PaymentQueryState.SEM_PROCURACAO: FederalConsultationOutcome.SEM_PROCURACAO,
    PaymentQueryState.FALHA: FederalConsultationOutcome.FALHA,
    PaymentQueryState.COTA_ESGOTADA: FederalConsultationOutcome.COTA_ESGOTADA,
    PaymentQueryState.INCERTO: FederalConsultationOutcome.INCERTO,
}


class PaymentQueryConflictError(IntegrationConflictError):
    """The panel was rendered against an older consultation, or one is running."""


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def receipt_request_key(payment_id: UUID) -> str:
    """Return the single SP-COMP identity of a payment: one receipt request, ever."""
    return _digest("pagtoweb-comprovante", str(payment_id))


def _page_key(query: PaymentQuery, index: int) -> str:
    return _digest("pagtoweb-pagina", query.request_key, str(index))


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


def open_payment_query(
    client: ClientCompany,
    window: PaymentRange,
    *,
    actor: User | None,
    nonce: str = "",
    cadence: date | None = None,
) -> PaymentQuery:
    """Create (or return) the query for this intent under the client's lock.

    A deliberate consultation is identified by its window and the form's nonce. A
    scheduled one (`cadence` = the competence month it covers) is identified by the
    client and that month alone: the window's end is the day the schedule fired,
    so it must not take part, or every later day would open another paid query.
    """
    key = (
        _digest("pagtoweb-agendada", str(client.pk), cadence.isoformat())
        if cadence is not None
        else _digest(
            "pagtoweb-consulta",
            str(client.pk),
            window.start.isoformat(),
            window.end.isoformat(),
            ",".join(window.revenue_codes),
            nonce,
        )
    )
    existing = PaymentQuery.objects.filter(client_id=client.pk, request_key=key).first()
    if existing is not None:
        return existing
    link = serpro_link(client)
    if link is None:
        msg = "Cliente sem vínculo ativo com a Receita (SERPRO)."
        raise PermissionDenied(msg)
    try:
        with transaction.atomic():
            query = PaymentQuery.objects.create(
                tenant_id=client.tenant_id,
                client=client,
                link=link,
                requested_by=actor,
                trigger="equipe" if actor is not None else "agendada",
                request_key=key,
                range_start=window.start,
                range_end=window.end,
                revenue_codes=list(window.revenue_codes),
                environment=link.connection.environment,
            )
    except IntegrityError:
        # The partial unique index admits one active consultation per client.
        msg = "Já existe uma consulta de pagamentos em andamento para este cliente."
        raise PaymentQueryConflictError(msg) from None
    record_event(
        action="integrations.payment_consultation_requested",
        tenant_id=client.tenant_id,
        actor=actor,
        obj=ObjectRef("PaymentQuery", str(query.pk)),
        metadata={
            "trigger": query.trigger,
            "range_start": window.start.isoformat(),
            "range_end": window.end.isoformat(),
        },
    )
    return query


def _require(actor: User, client: ClientCompany) -> None:
    if not tenant_has(client.tenant_id, FLAG) or not can(actor, CHECKS):
        raise PermissionDenied


def queue_payment_consultation(
    actor: User,
    client_id: UUID,
    window: PaymentRange,
    *,
    expected_latest: UUID | None,
    nonce: str,
) -> PaymentQuery:
    """Staff click: record the intent and enqueue the run once the request commits.

    The form carries the newest consultation it rendered; a caller acting on an
    older view, or while one is running, gets a typed conflict and writes nothing.
    """
    visible = load_visible_client(actor, client_id)
    _require(actor, visible)
    with transaction.atomic():
        client = ClientCompany.objects.select_for_update().get(pk=visible.pk)
        _require(actor, client)
        latest = (
            PaymentQuery.objects.filter(client_id=client.pk)
            .order_by("-created_at", "-pk")
            .first()
        )
        if (latest.pk if latest else None) != expected_latest or (
            latest is not None and latest.state in ACTIVE_QUERY_STATES
        ):
            msg = "A consulta foi atualizada por outra pessoa; recarregue a página."
            raise PaymentQueryConflictError(msg)
        query = open_payment_query(client, window, actor=actor, nonce=nonce)
    from apps.integrations.serpro.tasks import (  # noqa: PLC0415 - tasks import us
        run_payment_query_task,
    )

    tenant_id, query_id = str(query.tenant_id), str(query.pk)
    transaction.on_commit(lambda: run_payment_query_task.delay(tenant_id, query_id))
    return query


def consult_payments(
    client: ClientCompany,
    window: PaymentRange,
    *,
    actor: User | None,
    nonce: str,
) -> PaymentQuery:
    """Open and run one consultation now: the scheduled (system) path and its tests.

    Must run outside any transaction. With an actor the capability is checked
    first; without one the call is the firm's scheduled system consultation.
    """
    assert_no_transaction()
    with tenant_context(client.tenant_id):
        if actor is not None:
            client = load_visible_client(actor, client.pk)
            _require(actor, client)
        elif not tenant_has(client.tenant_id, FLAG):
            raise PermissionDenied
        locked = ClientCompany.objects.select_for_update().get(pk=client.pk)
        query = open_payment_query(locked, window, actor=actor, nonce=nonce)
    return run_payment_query(query.tenant_id, query.pk)


def _initiator_allowed(query: PaymentQuery) -> bool:
    actor = query.requested_by
    if actor is None:
        return True
    try:
        load_visible_client(actor, query.client_id)
    except Exception:  # noqa: BLE001 - any refusal means the initiator lost access
        return False
    return can(actor, CHECKS)


def _finish(
    query: PaymentQuery,
    state: str,
    code: str = "",
    *,
    receipt: ConsultationCall | None = None,
) -> PaymentQuery:
    """Record the consultation's end, its failure receipt and the owned work.

    Runs inside the client's tenant context.
    """
    if receipt is not None:
        record_payment_consultation(
            query.client, receipt, failure=_RECEIPT_OUTCOME[state]
        )
    query.state = state
    query.failure_code = code
    query.finished_at = timezone.now()
    query.version += 1
    # Settled: no runner holds the consultation any longer.
    query.settlement_owner = None
    query.settlement_until = None
    query.save()
    _release_unused(query)
    record_event(
        action="integrations.payment_consultation_finished",
        tenant_id=query.tenant_id,
        obj=ObjectRef("PaymentQuery", str(query.pk)),
        metadata={
            "state": state,
            "code": code,
            "pages": query.pages_fetched,
            "found": query.payments_found,
            "matched": query.matched_count,
        },
    )
    today = timezone.localdate()
    if state in {PaymentQueryState.FALHA, PaymentQueryState.INCERTO}:
        raise_problem(
            query.client,
            Problem(FAILED_REASON, query.pk, review_on=today, service=PAGE_OPERATION),
        )
    elif (
        state == PaymentQueryState.SEM_PROCURACAO
        and receipt is not None
        and not receipt.provider_ref
    ):
        raise_problem(
            query.client,
            Problem(
                IssueReason.AUTORIZACAO_AUSENTE,
                authority_subject(query.client_id, AuthorityService.PAGTOWEB),
                review_on=today,
                service=AuthorityService.PAGTOWEB,
            ),
        )
    logger.info(
        "PAGTOWEB consultation finished",
        extra={"state": state, "code": code, "pages": query.pages_fetched},
    )
    return query


def _release_unused(query: PaymentQuery) -> None:
    """Cancel the finished consultation's pages that were reserved but never claimed.

    Such a page (its runner yielded, or died before the claim) will never be sent
    once the consultation is final; its reservation goes back to the firm's cap in
    this same transaction. A claimed page is left alone: it is the protocol's.
    """
    keys = [_page_key(query, index) for index in range(MAX_PAGES)]
    for call in ProviderCall.objects.select_for_update().filter(
        client_id=query.client_id, request_key__in=keys, state=CallState.RESERVED
    ):
        release(call)
        call.state = CallState.CANCELLED
        call.provider_code = UNUSED_PAGE
        call.version += 1
        call.save()


def _locked(query_id: UUID) -> PaymentQuery:
    return (
        PaymentQuery.objects.select_for_update(of=("self",))
        .select_related("client", "link__connection", "requested_by")
        .get(pk=query_id)
    )


@contextmanager
def _as_runner(tenant_id: UUID, runner: UUID) -> Iterator[None]:
    """Tenant context whose writes the database attributes to `runner`."""
    with tenant_context(tenant_id):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config(%s, %s, true)", [OWNER_SETTING, str(runner)]
            )
        yield


@contextmanager
def _alive(runner: UUID) -> Iterator[None]:
    """Hold the runner's session lock for as long as this run lasts.

    Postgres drops it with the connection, so it outlives a stall but not the
    death of the process: the proof that a lapsed lease's owner is really gone.
    """
    with connection.cursor() as cursor:
        cursor.execute(RUNNER_LOCK, [str(runner)])
    try:
        yield
    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute(RUNNER_UNLOCK, [str(runner)])
        except (InterfaceError, OperationalError):
            # The connection is gone, and its session lock went with it.
            logger.warning("PAGTOWEB runner connection lost before unlocking")


class _OwnerAlive(Func):
    """The settlement owner's runner still holds its session lock (`_alive`)."""

    function = "integrations_payment_runner_alive"
    output_field = BooleanField()


def _free(runner: UUID) -> Q:
    """No other runner holds the settlement (Postgres evaluates it).

    Another runner's lease is free only once it has run out and that runner's
    connection is gone: a live owner stalled past its deadline keeps it.
    """
    return (
        Q(settlement_owner__isnull=True)
        | Q(settlement_owner=runner)
        | (Q(settlement_until__lte=Now()) & ~Q(_OwnerAlive("settlement_owner")))
    )


def _unowned(query_id: UUID, runner: UUID) -> PaymentQuery | None:
    """Lock an active consultation no live runner owns; None means yield."""
    return (
        PaymentQuery.objects.select_for_update(of=("self",))
        .select_related("client", "link__connection", "requested_by")
        .filter(pk=query_id, state__in=ACTIVE_QUERY_STATES)
        .filter(_free(runner))
        .first()
    )


def _open_page(query_id: UUID, index: int, runner: UUID) -> QuerySet[PaymentQuery]:
    """Page `index` of an active consultation that no other live runner settles.

    The lease (`_own`) and the settlement lock (`_settling`) share this condition;
    it belongs to the locking statement, so Postgres evaluates it on the row's
    newest version once the lock is granted.
    """
    return (
        PaymentQuery.objects.select_related("client", "link__connection")
        .filter(pk=query_id, state__in=ACTIVE_QUERY_STATES, pages_fetched=index)
        .filter(_free(runner))
    )


def _settling(query_id: UUID, index: int, runner: UUID) -> PaymentQuery | None:
    """Lock the consultation to settle page `index`; None when that is not ours.

    A runner that read the consultation before another one finished it, applied
    this page or took its settlement lease finds nothing to settle: a terminal
    outcome is final.
    """
    return (
        _open_page(query_id, index, runner)
        .select_related("requested_by")
        .select_for_update(of=("self",))
        .first()
    )


def _own(query: PaymentQuery, index: int, runner: UUID) -> bool:
    """Take or renew the settlement lease before paying for page `index`.

    One conditional UPDATE: under READ COMMITTED Postgres re-checks it on the
    newest row version, so of two runners racing for the same page exactly one
    becomes the owner and the other pays for nothing.
    """
    with _as_runner(query.tenant_id, runner):
        return bool(
            _open_page(query.pk, index, runner).update(
                settlement_owner=runner, settlement_until=Now() + SETTLEMENT_LEASE
            )
        )


def _stored(query_id: UUID) -> PaymentQuery:
    return PaymentQuery.objects.select_related("client").get(pk=query_id)


def _receipt(
    query: PaymentQuery, digest: str, provider_ref: str = ""
) -> ConsultationCall:
    return ConsultationCall(
        range_start=query.range_start,
        range_end=query.range_end,
        consulted_at=timezone.now(),
        environment=query.environment,
        provider_ref=provider_ref,
        scope_digest=digest,
    )


def _refused(  # noqa: PLR0913 - the page's identity plus the refusing runner
    query: PaymentQuery,
    index: int,
    digest: str,
    key: str,
    error: Exception,
    *,
    runner: UUID,
) -> PaymentQuery:
    """Admission refused this page before any byte was sent."""
    with _as_runner(query.tenant_id, runner):
        settling = _settling(query.pk, index, runner)
        if settling is None:
            return _stored(query.pk)
        query = settling
        row = ProviderCall.objects.filter(
            client_id=query.client_id, request_key=key
        ).first()
        ref = str(row.pk) if row is not None else ""
        if isinstance(error, BudgetDeniedError):
            if index == 0:
                return _finish(
                    query,
                    PaymentQueryState.COTA_ESGOTADA,
                    error.reason,
                    receipt=_receipt(query, digest, ref),
                )
            # The pages already fetched stay; the consultation is incomplete.
            record_payment_consultation(
                query.client,
                _receipt(query, digest, ref),
                failure=FederalConsultationOutcome.COTA_ESGOTADA,
            )
            return _finish(query, PaymentQueryState.FALHA, "cota_esgotada_parcial")
        if error.args and error.args[0] == CallState.AUTHORITY:
            return _finish(
                query,
                PaymentQueryState.SEM_PROCURACAO,
                "autorizacao_ausente",
                receipt=_receipt(query, digest, ref),
            )
        return _finish(
            query,
            PaymentQueryState.FALHA,
            "integracao_bloqueada",
            receipt=_receipt(query, digest, ref),
        )


def _settle_page(  # noqa: PLR0911, PLR0913 - one outcome per finalized call state
    query: PaymentQuery,
    index: int,
    digest: str,
    call_id: UUID,
    adapter: PageAdapter,
    *,
    runner: UUID,
) -> PaymentQuery | None:
    """Turn one finalized page call into receipts and payments; None = next page."""
    with _as_runner(query.tenant_id, runner):
        settling = _settling(query.pk, index, runner)
        if settling is None:
            return _stored(query.pk)
        query = settling
        call = ProviderCall.objects.get(pk=call_id, client_id=query.client_id)
        receipt = _receipt(query, digest, str(call.pk))
        if call.state == CallState.SUCCESS and adapter.records is not None:
            try:
                with transaction.atomic():
                    # Matching may bind a document already on file; it shares the
                    # page's transaction, so a refused page binds nothing.
                    records = match_das_payments(query.client, adapter.records)
                    record_payment_consultation(query.client, receipt, records)
            except (FederalRecordConflictError, ValidationError):
                return _finish(query, PaymentQueryState.FALHA, "documento_divergente")
            query.pages_fetched = index + 1
            query.payments_found += len(records)
            query.matched_count += sum(
                1 for record in records if record.matched_obligation_id
            )
            if len(records) < PAGE_SIZE:
                state = (
                    PaymentQueryState.ENCONTRADO
                    if query.payments_found
                    else PaymentQueryState.NADA_LOCALIZADO
                )
                return _finish(query, state)
            if query.pages_fetched == MAX_PAGES:
                return _finish(query, PaymentQueryState.FALHA, "limite_de_paginas")
            query.version += 1
            query.save()
            return None
        if call.state == CallState.SUCCESS:
            # Billed and finalized, but its transient page did not reach us. It is
            # never fetched again automatically: repeating it is a staff decision.
            return _finish(query, PaymentQueryState.INCERTO, "pagina_nao_registrada")
        if call.state == CallState.DENIED:
            record_authority_denial(
                query.client,
                PAGE_OPERATION,
                source_call_ref=str(call.pk),
                http_status=403,
            )
            return _finish(
                query,
                PaymentQueryState.SEM_PROCURACAO,
                call.provider_code,
                receipt=receipt,
            )
        if call.state == CallState.UNCERTAIN:
            return _finish(
                query, PaymentQueryState.INCERTO, call.provider_code, receipt=receipt
            )
        if call.state == CallState.QUOTA:
            return _refused_quota(query, index, receipt)
        return _finish(
            query, PaymentQueryState.FALHA, call.provider_code, receipt=receipt
        )


def _refused_quota(
    query: PaymentQuery, index: int, receipt: ConsultationCall
) -> PaymentQuery:
    if index == 0:
        return _finish(
            query, PaymentQueryState.COTA_ESGOTADA, "cota_esgotada", receipt=receipt
        )
    record_payment_consultation(
        query.client, receipt, failure=FederalConsultationOutcome.COTA_ESGOTADA
    )
    return _finish(query, PaymentQueryState.FALHA, "cota_esgotada_parcial")


def _page(  # noqa: PLR0911 - each step may end the consultation or yield it
    query: PaymentQuery, parties: Parties, index: int, runner: UUID
) -> PaymentQuery | None:
    """Fetch page `index` through the full protocol; None means fetch the next one."""
    data = page_data(query.window, index)
    digest = hashlib.sha256(
        build_request(PAGE_OPERATION, parties, data).body
    ).hexdigest()
    key = _page_key(query, index)
    request = CallRequest(PAGE_OPERATION, key, digest, key)
    with _as_runner(query.tenant_id, runner):
        current = _settling(query.pk, index, runner)
        if current is None:
            return _stored(query.pk)
        if not _initiator_allowed(current):
            return _finish(
                current, PaymentQueryState.FALHA, "solicitante_sem_permissao"
            )
    try:
        row = reserve(query.tenant_id, query.link_id, request)
    except AdmissionDeniedError as error:
        return _refused(query, index, digest, key, error, runner=runner)
    if not _own(query, index, runner):
        # A live runner owns this consultation's settlement; it is left alone. If
        # it was finished meanwhile, this page is never sent: release it now.
        with tenant_context(query.tenant_id):
            stored = _stored(query.pk)
            if stored.state not in ACTIVE_QUERY_STATES:
                _release_unused(stored)
            return stored
    ticket = claim(query.tenant_id, row.pk)
    if ticket is None:
        with tenant_context(query.tenant_id):
            state = ProviderCall.objects.values_list("state", flat=True).get(pk=row.pk)
        if state in {CallState.CLAIMED, CallState.STARTED}:
            return query  # Another worker holds this page's live lease.
        adapter = PageAdapter(
            SerproAdapter(
                _transport(cast("Environment", query.environment)), parties, data
            )
        )
        return _settle_page(query, index, digest, row.pk, adapter, runner=runner)
    adapter = PageAdapter(
        SerproAdapter(_transport(cast("Environment", query.environment)), parties, data)
    )
    perform(ticket, adapter)
    return _settle_page(query, index, digest, row.pk, adapter, runner=runner)


def run_payment_query(tenant_id: UUID, query_id: UUID) -> PaymentQuery:
    """Run (or resume) one consultation; each phase owns its own short transaction.

    A consultation whose settlement lease another runner holds is returned as
    stored, with nothing written: its owner finishes it, or a later delivery
    resumes it once that lease has run out and its owner's connection is gone.
    """
    assert_no_transaction()
    runner = uuid4()
    with _alive(runner):
        return _run(tenant_id, query_id, runner)


def _run(tenant_id: UUID, query_id: UUID, runner: UUID) -> PaymentQuery:
    with _as_runner(tenant_id, runner):
        query = _unowned(query_id, runner)
        if query is None:
            return _stored(query_id)
        if not _initiator_allowed(query):
            return _finish(query, PaymentQueryState.FALHA, "solicitante_sem_permissao")
        parties = termo_parties(query.link.connection_id, query.client.cnpj)
        if parties is None:
            return _finish(query, PaymentQueryState.SEM_PROCURACAO, "termo_ausente")
        if query.state == PaymentQueryState.NA_FILA:
            query.state = PaymentQueryState.EM_ANDAMENTO
            query.started_at = timezone.now()
            query.version += 1
            query.save()
    for index in range(query.pages_fetched, MAX_PAGES):
        finished = _page(query, parties, index, runner)
        if finished is not None:
            return finished
        with tenant_context(tenant_id):
            query = _locked(query_id)
    return query


def queue_payment_receipt(actor: User, client_id: UUID, payment_id: UUID) -> None:
    """Staff click: request this payment's receipt once the request commits."""
    client = load_visible_client(actor, client_id)
    _require(actor, client)
    payment = FederalPayment.objects.filter(pk=payment_id, client_id=client.pk).first()
    if payment is None:
        raise PermissionDenied
    record_event(
        action="integrations.payment_receipt_requested",
        tenant_id=client.tenant_id,
        actor=actor,
        obj=ObjectRef("FederalPayment", str(payment.pk)),
        metadata={},
    )
    from apps.integrations.serpro.tasks import (  # noqa: PLC0415 - tasks import us
        emit_payment_receipt_task,
    )

    args = (str(client.tenant_id), str(payment.pk), str(actor.pk))
    transaction.on_commit(lambda: emit_payment_receipt_task.delay(*args))


def emit_payment_receipt(
    tenant_id: UUID, payment_id: UUID, actor_id: UUID
) -> ProviderCall | None:
    """Issue COMPARRECADACAO72 once per payment and store its PDF privately.

    The request identity is the payment itself, so a repeated click or a
    redelivered task returns the existing call and never issues (or bills) again.
    """
    assert_no_transaction()
    with tenant_context(tenant_id):
        payment = FederalPayment.objects.select_related("client").get(pk=payment_id)
        actor = User.objects.get(pk=actor_id)
        try:
            client = load_visible_client(actor, payment.client_id)
            _require(actor, client)
        except Exception:  # noqa: BLE001 - a lost permission refuses with no rows
            return None
        link = serpro_link(client)
        parties = termo_parties(link.connection_id, client.cnpj) if link else None
        if link is None or parties is None:
            return None
        environment = link.connection.environment
    data: JSONValue = {"numeroDocumento": payment.numero_documento}
    digest = hashlib.sha256(
        build_request(RECEIPT_OPERATION, parties, data).body
    ).hexdigest()
    key = receipt_request_key(payment.pk)
    try:
        row = reserve(
            tenant_id, link.pk, CallRequest(RECEIPT_OPERATION, key, digest, key)
        )
    except AdmissionDeniedError:
        return None
    ticket = claim(tenant_id, row.pk)
    if ticket is None:
        return row
    adapter = ReceiptAdapter(
        SerproAdapter(_transport(cast("Environment", environment)), parties, data)
    )
    perform(ticket, adapter)
    with tenant_context(tenant_id):
        call = ProviderCall.objects.get(pk=row.pk)
        if call.state == CallState.DENIED:
            record_authority_denial(
                client,
                RECEIPT_OPERATION,
                source_call_ref=str(call.pk),
                http_status=403,
            )
    if call.state == CallState.SUCCESS and adapter.pdf is not None:
        try:
            ingest_system_document(
                tenant_id=tenant_id,
                client_id=client.pk,
                origin=AcquisitionOrigin.FEDERAL_PROVIDER,
                receipt_ref=str(call.pk),
                payload=adapter.pdf,
                filename=f"comprovante-pagamento-{payment.pk}.pdf",
                content_type="application/pdf",
                retention_class="fiscal_evidence",
            )
        except (UploadScannerOff, UploadRejected) as error:
            # The receipt was issued and billed; it is never requested again
            # automatically. The section shows "emitido, mas nao armazenado".
            logger.warning(
                "PAGTOWEB receipt not stored",
                extra={"code": type(error).__name__, "call": str(call.pk)},
            )
    return call
