"""Workspace actions on client authority: declare, verify, revoke (AXRM-008).

Each form carries the chain version it was rendered with; the service re-checks it,
and `fiscal.authority.manage`, under the client lock. Evidence goes through the staff
intake path (quarantine, scan, `upload_equipe`, portal-private). Its bytes are sniffed
before anything is stored, and only a PDF or an image is accepted -- never a
certificate file.
"""

from http import HTTPStatus
from typing import Final
from uuid import UUID

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.accounts.views import AuthenticatedRequest
from apps.authz.objects import load_visible_client
from apps.authz.services import require_can
from apps.clients.views import require_tenant
from apps.core.flags import tenant_has
from apps.core.refusal import WayBack, refusal_page
from apps.cycles.intake import (
    UploadMissing,
    UploadRejected,
    UploadScannerOff,
    UploadTooLarge,
    read_capped,
    staff_upload,
)
from apps.cycles.services.cycle import tenant_id
from apps.cycles.sniffing import JPEG, PDF, PNG, sniff
from apps.integrations.authority import (
    CAPTURE_FLAG,
    FEDERAL_FLAG,
    MANAGE_CAPABILITY,
    declare_authority,
    head_record,
    revoke_authority,
    verify_authority,
)
from apps.integrations.models import AUTHORITY_CATALOG, AuthorityService
from apps.integrations.services import IntegrationConflictError, InvalidTransitionError
from apps.integrations.workspace import CAPTURE_ROWS, FEDERAL_ROWS
from apps.tenants.models import Tenant

EVIDENCE_TYPES: Final = frozenset({PDF, PNG, JPEG})
CERTIFICATE_SUFFIXES: Final = (".pfx", ".p12", ".pem", ".key", ".cer", ".crt", ".der")
HEADING: Final = "Não foi possível registrar a autorização"


class VersionForm(forms.Form):
    """The chain version the page was rendered with."""

    expected_version = forms.IntegerField(min_value=0)


class DeclareForm(VersionForm):
    """A declaration: the catalog code, the validity and optional evidence."""

    code = forms.CharField(max_length=24)
    valid_from = forms.DateField()
    valid_to = forms.DateField(required=False)


def _workspace(client_id: UUID) -> str:
    return reverse("client-detail", args=[client_id]) + "#autorizacoes"


def _refuse(
    request: AuthenticatedRequest, client_id: UUID, status: int, reason: str
) -> HttpResponse:
    return refusal_page(
        request,
        status,
        heading=HEADING,
        reason=reason,
        back=WayBack(_workspace(client_id), "Voltar ao espaço do cliente"),
    )


def _require_service_enabled(tenant_id: UUID, service: str) -> None:
    if service not in AUTHORITY_CATALOG:
        raise Http404
    flag = FEDERAL_FLAG if service in FEDERAL_ROWS else CAPTURE_FLAG
    if service not in FEDERAL_ROWS + CAPTURE_ROWS or not tenant_has(tenant_id, flag):
        raise Http404


def _evidence_refusal(request: AuthenticatedRequest) -> str:
    """Return why the uploaded evidence is refused, or "" when it may be stored."""
    upload = request.FILES.get("evidence")
    if upload is None:
        return ""
    if (upload.name or "").lower().endswith(CERTIFICATE_SUFFIXES):
        return "Arquivos de certificado não são aceitos. Anexe a procuração em PDF."
    try:
        payload, _digest = read_capped(upload)
    except UploadTooLarge:
        return "O arquivo excede o tamanho máximo."
    except UploadMissing:
        return "O arquivo enviado está vazio."
    if sniff(payload) not in EVIDENCE_TYPES:
        return "Anexe a evidência em PDF (ou imagem PNG/JPEG)."
    return ""


def _act(
    request: AuthenticatedRequest, pk: UUID, service: str, action: str
) -> HttpResponse:
    # PLATFORM_QUERY_OK: the tenant this request already resolved, by its own id.
    tenant = Tenant.objects.get(pk=tenant_id())
    _require_service_enabled(tenant.pk, service)
    client = load_visible_client(request.user, pk)
    form_class = DeclareForm if action == "declarar" else VersionForm
    form = form_class(request.POST)
    if not form.is_valid():
        return _refuse(
            request, pk, HTTPStatus.UNPROCESSABLE_CONTENT, "Dados inválidos."
        )
    data = form.cleaned_data
    head = head_record(client.pk, service)
    if (head.version if head else 0) != data["expected_version"]:
        raise IntegrationConflictError
    try:
        with transaction.atomic():
            if action == "declarar":
                _declare(request, tenant, client.pk, service, form)
            elif action == "verificar":
                verify_authority(
                    request.user,
                    client.pk,
                    service=service,
                    expected_version=data["expected_version"],
                )
            else:
                revoke_authority(
                    request.user,
                    client.pk,
                    service=service,
                    expected_version=data["expected_version"],
                )
    except (ValidationError, UploadRejected, UploadScannerOff) as exc:
        reason = (
            " ".join(exc.messages)
            if isinstance(exc, ValidationError)
            else "O arquivo não pôde ser recebido para verificação."
        )
        return _refuse(request, pk, HTTPStatus.UNPROCESSABLE_CONTENT, reason)
    except InvalidTransitionError as exc:
        return _refuse(request, pk, HTTPStatus.CONFLICT, str(exc))
    return redirect(_workspace(pk))


def _declare(
    request: AuthenticatedRequest,
    tenant: Tenant,
    client_id: UUID,
    service: str,
    form: forms.Form,
) -> None:
    refusal = _evidence_refusal(request)
    if refusal:
        raise ValidationError(refusal)
    client = load_visible_client(request.user, client_id)
    upload = request.FILES.get("evidence")
    document = None
    if upload is not None:
        document, _relinked = staff_upload(
            request.user, upload, tenant=tenant, client=client
        )
    data = form.cleaned_data
    declare_authority(
        request.user,
        client_id,
        service=service,
        code=data["code"],
        valid_from=data["valid_from"],
        valid_to=data["valid_to"],
        expected_version=data["expected_version"],
        evidence_document=document,
    )


@require_http_methods(["POST"])
@login_required
@require_tenant
@require_can(MANAGE_CAPABILITY)
def authority_action(
    request: AuthenticatedRequest, pk: UUID, service: str, action: str
) -> HttpResponse:
    """Declare, verify or revoke one service's authority from the workspace."""
    if action not in {"declarar", "verificar", "revogar"}:
        raise Http404
    try:
        return _act(request, pk, AuthorityService(service).value, action)
    except ValueError as exc:
        raise Http404 from exc
    except IntegrationConflictError:
        return _refuse(
            request,
            pk,
            HTTPStatus.CONFLICT,
            "A autorização mudou depois que você abriu a página. Nada foi salvo. "
            "Recarregue antes de tentar novamente.",
        )
