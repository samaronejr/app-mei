"""Firm-side download of a client document, gated by its intake state (T12, S8.3).

The portfolio decides first: the document's client must be in the actor's book, or
404 (an unassigned staff accountant learns nothing). Then the intake decides:

- `verificado` and the later clean states (T13: `classificado`, `divergente`,
  `vinculado`): served from the final key;
- `recebido` / `quarentena`: 409, no byte -- the scan has not answered yet;
- `rejeitado_tipo` / `rejeitado_malware`: 403, no byte -- retained, never served;
- `verificacao_indisponivel` (scanner off or down past 24 h) and vault uploads that
  never had an intake (the portal vault predates scanning): GET shows a warning
  dialog and nothing else; only its POST (CSRF, `confirmar=1`) serves the bytes, and
  each such download appends a `document.unscanned_downloaded` event to every linked
  cycle. The portal never re-serves intake files that are not `verificado`.

Every served response goes through `apps.cycles.serving.document_response`.
"""

from http import HTTPStatus
from typing import Final
from uuid import UUID

import sentry_sdk
from django.contrib.auth.decorators import login_required
from django.core.files.storage import default_storage
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from apps.accounts.views import AuthenticatedRequest
from apps.authz.objects import load_visible_client
from apps.authz.services import require_can
from apps.clients.views import require_tenant
from apps.cycles.evidence import (
    CLEAN_STATES,
    PENDING_SCAN_STATES,
    REJECTED_STATES,
    DocumentIntake,
    EvidenceLink,
)
from apps.cycles.records import CycleEvent
from apps.cycles.services.cycle import require_enabled
from apps.cycles.serving import document_response
from apps.obligations.models import Document

DOCUMENTS_CAPABILITY: Final = "documents.transfer"
CONFIRM_FIELD: Final = "confirmar"


def _serve(document: Document, key: str) -> HttpResponse:
    try:
        with default_storage.open(key, "rb") as handle:
            payload = handle.read()
    except OSError as error:
        sentry_sdk.capture_exception(error)
        return HttpResponse(
            "Documento indisponível no momento.",
            status=HTTPStatus.SERVICE_UNAVAILABLE,
        )
    return document_response(
        payload,
        content_type=document.content_type,
        filename=document.original_filename,
    )


def _record_unscanned_download(
    request: AuthenticatedRequest, document: Document, intake: DocumentIntake | None
) -> None:
    for link in (
        EvidenceLink.objects.filter(document=document)
        .select_related("request__cycle")
        .order_by("created_at", "pk")
    ):
        cycle = link.request.cycle
        CycleEvent.objects.create(
            tenant_id=cycle.tenant_id,
            client_id=cycle.client_id,
            cycle=cycle,
            actor=request.user,
            type="document.unscanned_downloaded",
            aggregate_type="DocumentIntake" if intake else "Document",
            aggregate_id=intake.pk if intake else document.pk,
            aggregate_version=intake.version if intake else 1,
            payload={"document_id": str(document.pk)},
        )


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
@require_can(DOCUMENTS_CAPABILITY)
def staff_document_download(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Serve a client document to the firm only as its intake state allows."""
    require_enabled()
    document = Document.objects.filter(pk=pk).first()
    if document is None:
        raise Http404
    load_visible_client(request.user, document.client_id)
    intake = DocumentIntake.objects.filter(document=document).first()
    if intake is not None and intake.state in CLEAN_STATES:
        if request.method != "GET":
            raise Http404
        return _serve(document, document.storage_key)
    if intake is not None and intake.state in PENDING_SCAN_STATES:
        return HttpResponse(
            "Arquivo em verificação antivírus. Tente novamente em alguns minutos.",
            status=HTTPStatus.CONFLICT,
            content_type="text/plain; charset=utf-8",
        )
    if intake is not None and intake.state in REJECTED_STATES:
        return HttpResponse(
            "Arquivo recusado na verificação. Ele fica guardado e não é entregue.",
            status=HTTPStatus.FORBIDDEN,
            content_type="text/plain; charset=utf-8",
        )
    # Unscanned (verificacao_indisponivel, or a vault upload without an intake):
    # the warning first; the bytes only after its POST.
    if request.method == "POST" and request.POST.get(CONFIRM_FIELD) == "1":
        _record_unscanned_download(request, document, intake)
        return _serve(document, intake.object_key if intake else document.storage_key)
    return render(
        request,
        "cycles/document_unscanned.html",
        {"document": document, "intake": intake},
    )
