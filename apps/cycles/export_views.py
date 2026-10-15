"""Firm-only evidence and export pages; all mutations carry rendered versions."""

import json
from http import HTTPStatus
from uuid import UUID

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from django.http.response import HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.accounts.views import AuthenticatedRequest
from apps.authz.services import require_can
from apps.clients.views import require_tenant
from apps.core.refusal import WayBack, refusal_page, sentences
from apps.cycles.archive_response import ArchiveDescriptor, EvidenceArchiveResponse
from apps.cycles.errors import IllegalTransition, VersionConflict
from apps.cycles.evidence_index import capture_index, evidence_index
from apps.cycles.export_records import EvidenceExport, EvidenceIndex, ExportState
from apps.cycles.export_service import load_export, request_export
from apps.cycles.services.completion import propose_reopen
from apps.cycles.services.cycle import load_cycle

SECTION_TITLES = {
    "requests": "Solicitações",
    "links": "Vínculos de arquivos",
    "documents": "Originais",
    "intakes": "Verificações dos arquivos",
    "reviews": "Revisões da equipe",
    "fields": "Proveniência dos campos",
    "tasks": "Tarefas",
    "exceptions": "Pendências",
    "proposals": "Propostas",
    "approvals": "Aprovações",
    "executions": "Execuções",
    "messages": "Comunicações",
    "provider_responses": "Respostas do provedor",
    "delivery_effects": "Registros de envio",
    "publications": "Publicações",
    "acknowledgements": "Confirmações do cliente",
    "observations": "Observações de fontes",
    "events": "Revisões e eventos",
    "request_decisions": "Decisões das solicitações",
    "exception_evidence": "Referências das pendências",
    "amendments": "Emendas",
    "completions": "Conclusões anteriores",
}
EXPORT_ERRORS = {
    "source_changed": (
        "As evidências mudaram durante a geração. Solicite um novo arquivo."
    ),
    "source_busy": (
        "As evidências estão sendo alteradas. Solicite outro arquivo depois."
    ),
    "metadata_limit": (
        "O índice excede o limite de 8 MiB. Revise o escopo das evidências."
    ),
    "original_ineligible": (
        "Há originais sem verificação concluída. Confira os documentos."
    ),
    "original_unavailable": "Um original não está disponível. Confira os documentos.",
    "byte_cap": "O conjunto excede o limite de 500 MiB.",
    "hash_mismatch": "Um original não corresponde ao resumo registrado.",
    "size_mismatch": "O tamanho de um original não corresponde ao registro.",
    "storage_failed": "Não foi possível acessar o armazenamento.",
    "authorization_changed": (
        "O acesso mudou. Peça a uma pessoa autorizada um novo arquivo."
    ),
    "alternate_storage_name": "O armazenamento não confirmou a identidade do arquivo.",
}


class EvidenceForm(forms.Form):
    """Choose a bounded command, never an arbitrary storage or client identity."""

    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    action = forms.ChoiceField(choices=[("index", "index"), ("export", "export")])


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
@require_can("evidence.export")
def index_page(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Inspect the latest generation and request asynchronous byte work."""
    cycle = load_cycle(request.user, pk)
    error, status = "", 200
    if request.method == "POST":
        form = EvidenceForm(request.POST)
        if form.is_valid():
            try:
                if form.cleaned_data["action"] == "export":
                    export = request_export(
                        request.user,
                        pk,
                        expected_version=form.cleaned_data["expected_version"],
                    )
                    return redirect("evidence-export-detail", pk=export.pk)
                capture_index(
                    request.user,
                    pk,
                    expected_version=form.cleaned_data["expected_version"],
                )
                return redirect("evidence-index", pk=pk)
            except VersionConflict as exc:
                error, status = str(exc), 409
            except (ValidationError, IllegalTransition) as exc:
                error, status = sentences(exc), 422
        else:
            error, status = "Revise a ação e a revisão informadas.", 422
    try:
        generation = request.GET.get("geracao")
        selected = UUID(generation) if generation else None
    except ValueError as error:
        raise Http404 from error
    index = evidence_index(request.user, pk, index_id=selected)
    return render(
        request,
        "cycles/evidence_index.html",
        {
            "cycle": cycle,
            "index": index,
            "generations": EvidenceIndex.objects.filter(cycle=cycle).order_by(
                "-created_at", "-pk"
            )[:50],
            "error": error,
            "sections": [
                {"label": label, "count": len(index.data.get(key, []))}
                for key, label in SECTION_TITLES.items()
            ]
            if index
            else [],
            "index_text": json.dumps(index.data, ensure_ascii=False, indent=2)
            if index
            else "",
            "exports": EvidenceExport.objects.filter(cycle=cycle).order_by(
                "-created_at", "-pk"
            ),
        },
        status=status,
    )


@require_http_methods(["GET"])
@login_required
@require_tenant
@require_can("evidence.export")
def export_detail(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Show the authorized lifecycle state without opening archive storage."""
    export = load_export(request.user, pk)
    expired = export.expires_at is not None and export.expires_at <= timezone.now()
    return render(
        request,
        "cycles/export_detail.html",
        {
            "export": export,
            "expired": expired,
            "ready": export.state == ExportState.READY and not expired,
            "error": EXPORT_ERRORS.get(
                export.error_code, "Confira as evidências e solicite um novo arquivo."
            ),
        },
    )


@require_http_methods(["GET"])
@login_required
@require_tenant
@require_can("evidence.export")
def export_download(request: AuthenticatedRequest, pk: UUID) -> HttpResponseBase:
    """Authorize and materialize the descriptor before the byte-only iterator."""
    export = load_export(request.user, pk)
    heading = "Não foi possível baixar o arquivo"
    back = WayBack(
        reverse("evidence-export-detail", args=[export.pk]),
        "Voltar ao arquivo de evidências",
    )
    if export.expires_at is not None and export.expires_at <= timezone.now():
        return refusal_page(
            request,
            HTTPStatus.GONE,
            heading=heading,
            reason="O prazo deste download encerrou.",
            back=back,
        )
    if export.state != ExportState.READY:
        return refusal_page(
            request,
            HTTPStatus.CONFLICT,
            heading=heading,
            reason="O arquivo ainda não está disponível.",
            back=back,
        )
    return EvidenceArchiveResponse(
        ArchiveDescriptor(
            export.storage_key,
            f"evidencias-{export.pk}.zip",
            export.byte_size,
            export.sha256,
        )
    )


@require_http_methods(["POST"])
@login_required
@require_tenant
@require_can("cycles.reopen")
def reopen_cycle(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Propose a whole-cycle amendment for a separate exact approval."""
    heading = "Não foi possível propor a emenda do ciclo"
    back = WayBack(reverse("cycle-detail", args=[pk]), "Voltar ao ciclo")
    try:
        version = int(request.POST.get("expected_version", ""))
    except ValueError:
        # The posted revision is not a number; Python's own text is not a reason.
        return refusal_page(
            request,
            HTTPStatus.UNPROCESSABLE_CONTENT,
            heading=heading,
            reason="Revise os dados informados.",
            back=back,
        )
    try:
        proposal = propose_reopen(
            request.user,
            pk,
            reason=request.POST.get("reason", ""),
            affected_scope=(pk,),
            expected_version=version,
        )
    except (ValueError, ValidationError, IllegalTransition) as exc:
        return refusal_page(
            request,
            HTTPStatus.UNPROCESSABLE_CONTENT,
            heading=heading,
            reason=sentences(exc),
            back=back,
        )
    except VersionConflict as exc:
        return refusal_page(
            request, HTTPStatus.CONFLICT, heading=heading, reason=str(exc), back=back
        )
    return redirect("approval-card", pk=proposal.pk)
