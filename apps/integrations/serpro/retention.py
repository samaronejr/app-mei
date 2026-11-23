"""The fiscal dates of a SERPRO original, read from the same client's records.

AXRM-051 (plan todo 51, A11). The purge path (`apps.cycles.retention_tasks`) dates a
fiscal original by the cycles and obligations linked to it. A SERPRO original can have
neither, so its other references are read here and added, never substituted:

* every fiscal-situation report holding these bytes: the cycle of each review AND the
  report's emission date (a report without an emission date dates nothing);
* a PGMEI guide (SP-DAS): the competence of the cycle its request was recorded for;
* a PAGTOWEB receipt (SP-COMP): the collection date of the payment whose id is the
  request identity, when that date lies inside the payment's own consultation window.

Every lookup stays inside the original's client. When a reference cannot be resolved,
or identical bytes may have been reused by another acquisition, the reason is returned
and the caller keeps the original: the latest known date cannot prove that every use
of the bytes has expired.
"""

from collections import Counter
from uuid import UUID

from django.utils import timezone

from apps.cycles.federal_records import FederalReportReview
from apps.cycles.models import ServiceCycle
from apps.cycles.retention import RetentionAnchor
from apps.integrations.call_models import NO_BYTES_STATES, ProviderCall
from apps.integrations.serpro.pagtoweb import RECEIPT_OPERATION
from apps.integrations.serpro.payments import receipt_request_key
from apps.integrations.serpro.pgmei import OPERATION as GUIDE_OPERATION
from apps.integrations.serpro.pgmei import _requested
from apps.obligations.models import (
    AcquisitionOrigin,
    Document,
    FederalPayment,
    FiscalSituationReport,
)

Found = tuple[list[RetentionAnchor], list[str]]
REPORT_OPERATION = "SP-SIT-R"


def federal_document_anchors(document: Document) -> tuple[list[RetentionAnchor], str]:
    """Return every federal reference of an original and the first unresolved one."""
    anchors: list[RetentionAnchor] = []
    gaps: list[str] = []
    for report in FiscalSituationReport.objects.filter(
        client_id=document.client_id, document=document
    ):
        found, missing = _report_anchors(report)
        anchors += found
        gaps += missing
    if document.acquisition_origin == AcquisitionOrigin.FEDERAL_PROVIDER:
        found, missing = _acquisition_anchors(document)
        anchors += found
        gaps += missing
    if not document.portal_visible:
        # Intake reuses private bytes whatever their first origin, so any private
        # original of the client may be the one an unaccounted acquisition reused.
        gaps += _shared_bytes(document)
    return anchors, gaps[0] if gaps else ""


def _report_anchors(report: FiscalSituationReport) -> Found:
    reviewed = [
        RetentionAnchor("revisao_ciclo", period_end, cycle_id)
        for cycle_id, period_end in FederalReportReview.objects.filter(
            client_id=report.client_id, report=report
        ).values_list("cycle_id", "cycle__period_end")
    ]
    if report.emitted_at is None:
        # A request date is not evidence of when these bytes were emitted.
        return reviewed, ["emissao_ausente"]
    try:
        emitted = timezone.localdate(report.emitted_at)
    except (OverflowError, ValueError):  # a stored instant with no local date
        return reviewed, ["data_fora_do_intervalo"]
    return [*reviewed, RetentionAnchor("emissao_relatorio", emitted, report.pk)], []


def _acquisition_anchors(document: Document) -> Found:
    try:
        call_id = UUID(document.acquisition_receipt_ref)
    except ValueError:
        return [], ["recibo_invalido"]
    call = ProviderCall.objects.filter(pk=call_id, client_id=document.client_id).first()
    if call is None:
        return [], ["chamada_ausente"]
    if call.state in NO_BYTES_STATES:
        return [], ["chamada_sem_bytes"]  # it never stored the bytes it is named for
    return _call_anchors(call, document)


def _call_anchors(call: ProviderCall, document: Document) -> Found:
    if call.operation == GUIDE_OPERATION:
        return _guide_anchors(call)
    if call.operation == RECEIPT_OPERATION:
        return _receipt_anchors(call)
    if call.operation == REPORT_OPERATION:
        # Only this call's own report, holding this original, dates its bytes.
        held = FiscalSituationReport.objects.filter(
            client_id=document.client_id,
            document=document,
            call_replies__has_key=str(call.pk),
        ).exists()
        return [], [] if held else ["relatorio_ausente"]
    return [], ["operacao_nao_reconhecida"]


def _guide_anchors(call: ProviderCall) -> Found:
    try:
        requested = _requested(call)
    except ValueError:  # the event's object id is plain text, e.g. a restored row
        return [], ["pedido_invalido"]
    cycle = (
        None
        if requested is None
        else ServiceCycle.objects.filter(
            pk=requested[0], client_id=call.client_id
        ).first()
    )
    if cycle is None:
        return [], ["pedido_sem_ciclo"]
    return [RetentionAnchor("competencia_guia", cycle.period_start, cycle.pk)], []


def _receipt_anchors(call: ProviderCall) -> Found:
    payments = FederalPayment.objects.filter(client_id=call.client_id).select_related(
        "consultation"
    )
    for payment in payments:
        if receipt_request_key(payment.pk) != call.request_key:
            continue
        window = payment.consultation
        start, end = window.range_start, window.range_end
        if start is None or end is None or not start <= payment.data_arrecadacao <= end:
            return [], ["data_contraditoria"]
        anchor = RetentionAnchor(
            "arrecadacao_pagamento", payment.data_arrecadacao, payment.pk
        )
        return [anchor], []
    return [], ["pagamento_ausente"]


def _shared_bytes(document: Document) -> list[str]:
    """Flag bytes another acquisition may share without a recorded association.

    Intake deduplicates identical private content, so any guide, receipt or report
    call that may have stored bytes and is not accounted for (an original of its
    own, or the report that recorded it holding a document) may have reused this
    original. Each original is decided by these calls, never by another original's
    claim to one of them.
    """
    client_id = document.client_id
    claimants = Counter(
        Document.objects.filter(
            client_id=client_id, acquisition_origin=AcquisitionOrigin.FEDERAL_PROVIDER
        ).values_list("acquisition_receipt_ref", flat=True)
    )
    calls = (
        ProviderCall.objects.filter(
            client_id=client_id,
            operation__in=(GUIDE_OPERATION, RECEIPT_OPERATION, REPORT_OPERATION),
        )
        .exclude(state__in=NO_BYTES_STATES)
        .values_list("pk", "operation")
    )
    for call_id, operation in calls:
        if operation == REPORT_OPERATION:
            # Only the report that recorded the call proves where its bytes went; a
            # provider row merely naming the call does not (it may be a restore).
            accounted = FiscalSituationReport.objects.filter(
                client_id=client_id,
                document__isnull=False,
                call_replies__has_key=str(call_id),
            ).exists()
        else:
            # A guide or receipt stores new bytes under the one original naming
            # it; a call two originals claim has no single proven owner.
            accounted = claimants[str(call_id)] == 1
        if not accounted:
            return ["proveniencia_incompleta"]
    return []
