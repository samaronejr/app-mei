"""Schedule the monthly federal checks fairly, within every guard, and say how it went.

AXRM-018 (plan todo 18; A6 cadence, PM-9). Two daily beat entries share this module:
`federal-sitfis-monthly` asks for one SITFIS report for each client whose cycle opened
this month and has none yet; `federal-payments-monthly` opens one PAGTOWEB
consultation per client and month. Both reuse the delivered entry points
(`request_situation_report`, `open_payment_query`) and change neither their retry
limits nor their billing.

* Every claim is refused, with no row and no provider I/O, unless the platform is
  not paused (`PROVIDERS_PAUSED`/`SERPRO_PAUSED`), the environment is approved, the
  firm holds the `federal_checks` flag and the client's authority is active; the
  budget is admitted by the call protocol before any byte is sent.
* A run handles at most `CLIENT_LIMIT` clients per firm, starting after the firm's
  cursor and wrapping, and moves the cursor after each client. A worker that dies
  resumes at the client in flight (its monthly identity makes the retry free), and
  a client that cannot be served never holds the others back.
* The scheduler heartbeat records an attempt first and `last_success_at` only when
  every firm's turn completed; a pause or a failed turn never advertises success.
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from enum import StrEnum
from typing import TYPE_CHECKING, Final

import sentry_sdk
from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.clients.models import ClientCompany
from apps.clients.models.company import ClientStatus
from apps.core.flags import tenant_has
from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.cycles.models import ServiceCycle
from apps.cycles.openers import CLOSED_STATES
from apps.delivery.models import AutomationPause
from apps.integrations.authority import (
    OPERATION_REQUIREMENTS,
    Admission,
    Problem,
    authority_for,
    authority_subject,
    raise_problem,
)
from apps.integrations.models import ClientProviderLink, IssueReason, LinkStatus
from apps.integrations.serpro.models import PaymentQuery, PaymentQueryState
from apps.integrations.serpro.pagtoweb import PAGE_OPERATION
from apps.integrations.serpro.parties import termo_parties
from apps.integrations.serpro.payments import (
    FLAG,
    PaymentQueryConflictError,
    open_payment_query,
    serpro_link,
)
from apps.integrations.serpro.sitfis import (
    PROTOCOL_OPERATION,
    SituationReportRefusedError,
    request_situation_report,
)
from apps.obligations.models import FiscalSituationReport, SchedulerHeartbeat

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date
    from uuid import UUID

    from django.db.models import QuerySet

    from apps.core.tasks import PlatformTask
    from apps.integrations.serpro.pagtoweb import PaymentRange

logger = logging.getLogger(__name__)

CLIENT_LIMIT: Final = 20
RUNNING: Final = "federal_sweep_running"
FAILED: Final = "federal_sweep_failed"
PAUSED: Final = "pausado"
UNAPPROVED: Final = "ambiente_nao_aprovado"
_SITFIS_CURSOR: Final = "federal_sitfis_cursor"
_PAYMENTS_CURSOR: Final = "federal_payments_cursor"
_BLOCKED_CLIENTS: Final = (ClientStatus.SUSPENDED, ClientStatus.CLOSED)


class Outcome(StrEnum):
    """What one client's turn did."""

    QUEUED = "queued"  # a consultation or report was opened and handed to a worker
    EXISTING = "existing"  # this month's one already exists; nothing new
    REFUSED = "refused"  # a guard declined before any row or provider I/O


def global_refusal() -> str:
    """Name the platform-level reason no federal check may start now, else ''."""
    if settings.PROVIDERS_PAUSED or settings.SERPRO_PAUSED:
        return PAUSED
    if (
        settings.SERPRO_ENVIRONMENT == "producao"
        and settings.PROVIDER_REAL_DATA_APPROVED is not True
    ):
        return UNAPPROVED
    return ""


def _surface_missing_authority(client: ClientCompany, operation: str) -> None:
    """Open the owned work for a client the sweep could not serve for want of it."""
    service = OPERATION_REQUIREMENTS[operation].service or ""
    with transaction.atomic():
        raise_problem(
            client,
            Problem(
                IssueReason.AUTORIZACAO_AUSENTE,
                authority_subject(client.pk, service),
                review_on=timezone.localdate(),
                service=service,
            ),
        )


# Runs opened by the client being served; released once its transaction commits,
# because no provider-bound task may be handed over inside a transaction.
_pending: list[tuple[str, str]] = []


def _release_runs() -> None:
    from apps.integrations.serpro.tasks import (  # noqa: PLC0415 - tasks import us
        run_payment_query_task,
    )

    runs, _pending[:] = list(_pending), []
    for tenant_id, query_id in runs:
        run_payment_query_task.delay(tenant_id, query_id)


def payments_for(client: ClientCompany, window: PaymentRange) -> Outcome:
    """Open (or find) this month's consultation for one client and queue its run."""
    link = serpro_link(client)
    if link is None or termo_parties(link.connection_id, client.cnpj) is None:
        return Outcome.REFUSED
    decision = authority_for(client, PAGE_OPERATION, link=link)
    if not decision.usable:
        if decision.state in {Admission.MISSING, Admission.EXPIRED}:
            _surface_missing_authority(client, PAGE_OPERATION)
        return Outcome.REFUSED
    try:
        query = open_payment_query(client, window, actor=None, cadence=window.start)
    except PaymentQueryConflictError:
        return Outcome.EXISTING
    if query.state != PaymentQueryState.NA_FILA:
        return Outcome.EXISTING
    _pending.append((str(query.tenant_id), str(query.pk)))
    return Outcome.QUEUED


def report_for(client: ClientCompany) -> Outcome:
    """Request this month's SITFIS report for one client (the worker task emits it)."""
    try:
        request_situation_report(client)
    except SituationReportRefusedError as refused:
        if refused.code == "sem_procuracao":
            _surface_missing_authority(client, PROTOCOL_OPERATION)
        return Outcome.REFUSED
    return Outcome.QUEUED


def _linked() -> QuerySet[ClientCompany]:
    """Clients with an active SERPRO link on this deployment's environment."""
    links = ClientProviderLink.objects.filter(
        connection__provider="serpro",
        connection__environment=settings.SERPRO_ENVIRONMENT,
        status=LinkStatus.ATIVA,
    )
    return ClientCompany.objects.filter(pk__in=links.values("client_id")).exclude(
        status__in=_BLOCKED_CLIENTS
    )


def payment_candidates(window: PaymentRange) -> QuerySet[ClientCompany]:
    """Linked clients without a settled scheduled consultation for the month."""
    settled = PaymentQuery.objects.filter(
        client_id=OuterRef("pk"), trigger="agendada", range_start=window.start
    ).exclude(state=PaymentQueryState.NA_FILA)
    return _linked().filter(~Exists(settled))


def report_candidates(today: date) -> QuerySet[ClientCompany]:
    """Linked clients whose cycle opened this month and that have no report yet."""
    month = today.replace(day=1)
    opened = ServiceCycle.objects.filter(
        client_id=OuterRef("pk"), period_start=month
    ).exclude(state__in=CLOSED_STATES)
    asked = FiscalSituationReport.objects.filter(
        client_id=OuterRef("pk"),
        requested_at__gte=timezone.make_aware(datetime.combine(month, time.min)),
    )
    return _linked().filter(Exists(opened), ~Exists(asked))


def _pick(
    tenant_id: UUID, field: str, candidates: Callable[[], QuerySet[ClientCompany]]
) -> tuple[UUID, list[ClientCompany]]:
    """The firm's next turn: CLIENT_LIMIT clients after its cursor, wrapping."""
    control, _ = AutomationPause.objects.get_or_create(tenant_id=tenant_id)
    cursor = getattr(control, field)
    # Built here: a scoped queryset binds the tenant it is created under.
    ordered = candidates().order_by("pk")
    picked = list(
        (ordered.filter(pk__gt=cursor) if cursor else ordered)[:CLIENT_LIMIT]
    )
    if cursor and len(picked) < CLIENT_LIMIT:
        picked += list(ordered.filter(pk__lte=cursor)[: CLIENT_LIMIT - len(picked)])
    return control.pk, picked


def _advance(control_id: UUID, field: str, client_id: UUID) -> None:
    AutomationPause.objects.filter(pk=control_id).update(**{field: client_id})


def _turn(
    tenant_id: UUID,
    field: str,
    candidates: Callable[[], QuerySet[ClientCompany]],
    act: Callable[[ClientCompany], Outcome],
) -> tuple[int, int]:
    """One firm's bounded turn; returns (queued, failed clients).

    Each client is one short transaction that holds its work and the cursor move
    together, so a worker that dies there leaves neither: the next run starts at
    that client. Provider-bound runs are queued only after that commit.
    """
    with tenant_context(tenant_id):
        control_id, picked = _pick(tenant_id, field, candidates)
    queued = failed = 0
    for client in picked:
        try:
            with tenant_context(tenant_id):
                outcome = act(client)
                _advance(control_id, field, client.pk)
        except Exception as error:  # one client's failure never stops the firm
            failed += 1
            sentry_sdk.capture_exception(error)
            logger.warning("federal sweep failed for one client", exc_info=True)
            _pending.clear()
            # It stays a candidate on the next lap; a stuck client must not starve
            # the rest, so the cursor still moves past it.
            with tenant_context(tenant_id):
                _advance(control_id, field, client.pk)
        else:
            queued += outcome is Outcome.QUEUED
            _release_runs()
    return queued, failed


def _stamp(name: str, **fields: object) -> None:
    SchedulerHeartbeat.objects.update_or_create(name=name, defaults=fields)


def _sweep(
    task: PlatformTask,
    name: str,
    field: str,
    candidates: Callable[[], QuerySet[ClientCompany]],
    act: Callable[[ClientCompany], Outcome],
) -> int:
    assert_no_transaction()
    _stamp(name, last_attempt_at=timezone.now())
    refusal = global_refusal()
    if refusal:
        _stamp(name, last_error_code=refusal)
        return 0
    _stamp(name, last_error_code=RUNNING)
    queued = failures = 0
    for tenant_id in task.tenant_ids():
        with tenant_context(tenant_id):
            enabled = tenant_has(tenant_id, FLAG)
        if enabled:
            done, failed = _turn(tenant_id, field, candidates, act)
            queued += done
            failures += failed
    if failures:
        _stamp(name, last_error_code=FAILED)
    else:
        _stamp(name, last_error_code="", last_success_at=timezone.now())
    return queued


def sweep_payments(task: PlatformTask, window: PaymentRange) -> int:
    """Open this month's PAGTOWEB consultations for every firm, fairly."""
    return _sweep(
        task,
        "schedule_payment_consultations",
        _PAYMENTS_CURSOR,
        lambda: payment_candidates(window),
        lambda client: payments_for(client, window),
    )


def sweep_reports(task: PlatformTask, today: date) -> int:
    """Request this month's SITFIS reports for every firm, fairly."""
    return _sweep(
        task,
        "schedule_situation_reports",
        _SITFIS_CURSOR,
        lambda: report_candidates(today),
        lambda client: report_for(client),  # noqa: PLW0108 - resolved at call time
    )
