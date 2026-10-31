"""Record the accountant's review of an emitted SITFIS report (AXRM-016).

The firm-side recipe: portfolio-scoped cycle load under its row lock, an
optimistic version check that refuses before any write, `can()` re-checked
inside the lock, then the append-only review, one minimized `CycleEvent` and,
for `pendencias_registradas`, one owned `pendencia_fiscal_federal` exception per
listed item (owner: the engagement responsible). The report must be emitted and
its exact bytes must have passed the ordinary scan before anyone reviews it; the
PDF is never parsed and no status is derived from it.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import TYPE_CHECKING, Final

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from apps.cycles.capabilities import CYCLES_REVIEW
from apps.cycles.errors import VersionConflict
from apps.cycles.evidence import CLEAN_STATES, ScanVerdict
from apps.cycles.federal_records import (
    MAX_PENDENCIA_LENGTH,
    MAX_PENDENCIAS,
    FederalReportOutcome,
    FederalReportReview,
)
from apps.cycles.openers import owner_for
from apps.cycles.records import CycleEvent, ServiceCycle
from apps.cycles.services.cycle import check_version, load_cycle, require_actor
from apps.cycles.services.exceptions import ExceptionReason, open_exception
from apps.obligations.models import (
    AcquisitionOrigin,
    Document,
    FiscalReportState,
    FiscalSituationReport,
)

if TYPE_CHECKING:
    from uuid import UUID

    from apps.accounts.models import User

PENDENCIA_REASON: Final = "pendencia_fiscal_federal"
REASON: Final = ExceptionReason(
    "Trate a pendência registrada na revisão do relatório de situação fiscal da "
    "Receita e registre o que foi feito antes de encerrar.",
    label="pendência fiscal federal",
)


def report_input_digest(report: FiscalSituationReport) -> str:
    """Pin the report and the exact stored bytes a reviewer decides on."""
    document = report.document
    if document is None or report.emitted_at is None:
        msg = "O relatório ainda não foi emitido."
        raise ValidationError(msg)
    parts = (
        "sitfis-revisao-v1",
        str(report.pk),
        document.sha256,
        report.emitted_at.isoformat(),
    )
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _scanned(document: Document) -> bool:
    """Tell whether a firm-only federal original passed a clean scan."""
    return (
        not document.portal_visible
        and document.acquisition_origin == AcquisitionOrigin.FEDERAL_PROVIDER
        and document.purged_at is None
        and document.intakes.filter(
            state__in=CLEAN_STATES,
            verdict=ScanVerdict.CLEAN,
            scanned_at__isnull=False,
        ).exists()
    )


def _pendencias(outcome: str, items: list[str]) -> list[str]:
    """Pendencias exist exactly for `pendencias_registradas`, each non-empty."""
    if outcome not in FederalReportOutcome.values:
        msg = "Escolha o resultado da revisão."
        raise ValidationError(msg)
    cleaned = [item.strip() for item in items if isinstance(item, str)]
    if len(cleaned) != len(items) or any(not item for item in cleaned):
        msg = "Descreva cada pendência registrada."
        raise ValidationError(msg)
    if outcome == FederalReportOutcome.PENDENCIAS:
        if not cleaned:
            msg = "Registre ao menos uma pendência."
            raise ValidationError(msg)
    elif cleaned:
        msg = "Pendências só acompanham o resultado com pendências registradas."
        raise ValidationError(msg)
    if len(cleaned) > MAX_PENDENCIAS or any(
        len(item) > MAX_PENDENCIA_LENGTH for item in cleaned
    ):
        msg = "Use no máximo 50 pendências de até 500 caracteres."
        raise ValidationError(msg)
    return cleaned


def _subject(review: FederalReportReview, index: int) -> UUID:
    """One stable exception subject per listed pendencia of one review."""
    return uuid.uuid5(review.pk, f"pendencia-{index}")


def record_federal_report_review(  # noqa: PLR0913 - the rendered form's fields
    actor: User,
    cycle_id: UUID,
    report_id: UUID,
    *,
    outcome: str,
    pendencias: list[str],
    expected_version: int,
    input_digest: str,
) -> FederalReportReview:
    """Append one review; every refusal precedes every write."""
    items = _pendencias(outcome, pendencias)
    with transaction.atomic():
        cycle = load_cycle(actor, cycle_id, lock=True)
        # No row lock: an emitted report is never written again, and the SITFIS
        # worker locks report before cycle; locking it here (cycle first) would
        # invert that order. A not-yet-emitted report is refused below.
        report = (
            FiscalSituationReport.objects.select_related("document")
            .filter(pk=report_id, client_id=cycle.client_id)
            .first()
        )
        if report is None:
            raise Http404
        check_version(cycle.version, expected_version)
        require_actor(actor, CYCLES_REVIEW)
        if report.state != FiscalReportState.EMITIDO or report.document is None:
            msg = "O relatório ainda não foi emitido."
            raise ValidationError(msg)
        if not _scanned(report.document):
            msg = "O relatório ainda não passou pela verificação antivírus."
            raise ValidationError(msg)
        if input_digest != report_input_digest(report):
            msg = "O relatório mudou. Recarregue antes de salvar."
            raise VersionConflict(msg)
        owner = None
        if outcome == FederalReportOutcome.PENDENCIAS:
            owner = cycle.engagement.responsible or owner_for(cycle)
            if owner is None:
                msg = "Defina o responsável pelo serviço antes de registrar pendências."
                raise ValidationError(msg)
        review = FederalReportReview.objects.create(
            tenant_id=cycle.tenant_id,
            client_id=cycle.client_id,
            report=report,
            cycle=cycle,
            outcome=outcome,
            pendencias=items,
            reviewer=actor,
            reviewed_at=timezone.now(),
            input_digest=input_digest,
        )
        cycle.version += 1
        cycle.save(update_fields=["version"])
        CycleEvent.objects.create(
            tenant_id=cycle.tenant_id,
            client_id=cycle.client_id,
            cycle=cycle,
            actor=actor,
            type="federal.report.reviewed",
            aggregate_type="FederalReportReview",
            aggregate_id=review.pk,
            aggregate_version=1,
            payload={
                "report_id": str(report.pk),
                "review_id": str(review.pk),
                "outcome": outcome,
                "pendencias": len(items),
            },
        )
        if owner is not None:
            for index in range(len(items)):
                version = ServiceCycle.objects.values_list("version", flat=True).get(
                    pk=cycle.pk
                )
                open_exception(
                    PENDENCIA_REASON,
                    _subject(review, index),
                    actor=None,
                    cycle_id=cycle.pk,
                    expected_version=version,
                    owner=owner,
                    review_on=timezone.localdate(),
                    evidence_refs=(review.pk,),
                )
        return review


def review_statement(
    review: FederalReportReview, report: FiscalSituationReport
) -> str | None:
    """Return the dated sentence only a `sem_pendencias_nas_fontes` review shows.

    Every other outcome has no assurance sentence at all (A9: never "sem
    pendencias" without the sources and their date, never "regular").
    """
    if (
        review.outcome != FederalReportOutcome.NADA_REGISTRADO
        or report.emitted_at is None
    ):
        return None
    on = timezone.localtime(report.emitted_at).strftime("%d/%m/%Y")
    return f"Nenhuma pendência registrada nas fontes verificadas em {on}."
