"""Portal requests inbox, upload against a request, company switcher and branding (T11).

Every view but `company_select` runs inside `PortalMiddleware`'s transaction as
`app_portal`, confined to the one client the verified membership names. The request
tables (`cycles_evidencerequest`, `cycles_evidencelink`, `cycles_documentintake`) are
on the portal SELECT allow-list with RESTRICTIVE client policies, so no view here
filters by client: a foreign request id matches no row and answers 404, and a test
that drops the policy must be able to see the leak.

`RequestInternal` and the cycle's internal notes are never read here: the portal role
holds no privilege on either table, so reaching for them would be `permission denied`
rather than a leak -- and the templates are handed scalars only.

Everything is behind `cycles_r1`, answered from the portal stash without a query:
unflagged tenants get 404 on every route below and today's pages byte for byte.
"""

from dataclasses import dataclass
from datetime import date
from http import HTTPStatus
from typing import Final
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.files.storage import default_storage
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.authz.services import require_can
from apps.core.flags import tenant_has, tenant_row_has
from apps.cycles.evidence import (
    CLIENT_OPEN_STATES,
    CLIENT_VISIBLE_STATES,
    EvidenceLink,
    EvidenceRequest,
)
from apps.cycles.intake import UploadMissing, UploadTooLarge, intake_upload
from apps.cycles.services.requests import client_status
from apps.portal.hosts import portal_slug_from_host
from apps.portal.middleware import SELECTED_CLIENT_SESSION_KEY, PortalHttpRequest
from apps.portal.views import VAULT_CAPABILITY, PortalVaultRequest, _size_label
from apps.tenants.models import Membership, Tenant

HX_REQUEST: Final = "HX-Request"
FLAG: Final = "cycles_r1"

# Chip copy (S5.7). "Recebido" is a client submission awaiting the team's check: it
# never takes the ok/verified style or any wording of certainty.
STATUS_LABELS: Final[dict[str, str]] = {
    "solicitado": "Pendente",
    "rejeitado": "Enviar novamente",
    "atrasada": "Atrasada",
    "recebido": "Recebido",
    "recebido_pela_equipe": "Com a equipe",
    "em_revisao": "Em conferência",
    "aceito": "Conferido pela equipe",
}
STATUS_HINTS: Final[dict[str, str]] = {
    "recebido": "Recebemos seu arquivo. A equipe ainda vai conferir.",
    "atrasada": "O prazo passou. Envie assim que puder.",
    "solicitado": "Envie o arquivo pelo botão abaixo.",
}


@dataclass(frozen=True, slots=True)
class RequestRow:
    """One request flattened to scalars: templates never receive the row."""

    id: UUID
    title: str
    client_note: str
    due_on: date | None
    status: str
    status_label: str
    hint: str
    open: bool


@dataclass(frozen=True, slots=True)
class SentFile:
    """One file the client sent against a request."""

    name: str
    size_label: str
    sent_at: object
    relinked: bool


def _require_flag(request: HttpRequest) -> None:
    tenant = getattr(request, "tenant", None)
    if tenant is None or not tenant_has(tenant.pk, FLAG):
        raise Http404


def _row(request: EvidenceRequest, *, submitted: bool, today: date) -> RequestRow:
    status = client_status(request, submitted=submitted, today=today)
    return RequestRow(
        id=request.pk,
        title=request.title,
        client_note=request.client_note,
        due_on=request.due_on,
        status=status,
        status_label=STATUS_LABELS.get(status, "Pendente"),
        hint=STATUS_HINTS.get(status, ""),
        open=request.state in CLIENT_OPEN_STATES,
    )


def open_requests(today: date) -> list[RequestRow]:
    """Return the client's open requests with their derived chips, in two queries.

    No client filter: the RESTRICTIVE policy confines both tables to this client.
    """
    rows = list(
        EvidenceRequest.objects.filter(state__in=CLIENT_OPEN_STATES).order_by(
            "due_on", "created_at", "pk"
        )
    )
    submitted = set(
        EvidenceLink.objects.filter(request_id__in=[row.pk for row in rows])
        .values_list("request_id", flat=True)
        .distinct()
    )
    return [_row(row, submitted=row.pk in submitted, today=today) for row in rows]


def _visible_request(pk: UUID) -> EvidenceRequest:
    return get_object_or_404(
        EvidenceRequest.objects.filter(state__in=CLIENT_VISIBLE_STATES), pk=pk
    )


def _files(request: EvidenceRequest) -> list[SentFile]:
    links = (
        EvidenceLink.objects.filter(request=request)
        .select_related("document")
        .order_by("created_at", "pk")
    )
    return [
        SentFile(
            name=link.document.original_filename,
            size_label=_size_label(link.document.byte_size),
            sent_at=link.created_at,
            relinked=link.relinked,
        )
        for link in links
    ]


def _status_context(request: EvidenceRequest) -> dict[str, object]:
    files = _files(request)
    return {
        "req": _row(request, submitted=bool(files), today=timezone.localdate()),
        "files": files,
    }


@require_http_methods(["GET"])
@login_required
@require_can("clients.view_assigned")
def request_detail(request: PortalVaultRequest, pk: UUID) -> HttpResponse:
    """One request: what is asked, by when, what was sent, and the upload control."""
    _require_flag(request)
    evidence_request = _visible_request(pk)
    return render(
        request,
        "portal/request.html",
        {
            **_status_context(evidence_request),
            "cycles_r1": True,
            "max_bytes": settings.PORTAL_UPLOAD_MAX_BYTES,
            "max_size_label": _size_label(settings.PORTAL_UPLOAD_MAX_BYTES),
        },
    )


def _refused(request: PortalVaultRequest, pk: UUID, message: str) -> HttpResponse:
    """400 in pt-BR: plain text for the upload component, a full page without it."""
    evidence_request = _visible_request(pk)
    if request.headers.get(HX_REQUEST):
        return HttpResponse(
            message,
            status=HTTPStatus.BAD_REQUEST,
            content_type="text/plain; charset=utf-8",
        )
    return render(
        request,
        "portal/request.html",
        {
            **_status_context(evidence_request),
            "cycles_r1": True,
            "error": message,
            "max_bytes": settings.PORTAL_UPLOAD_MAX_BYTES,
            "max_size_label": _size_label(settings.PORTAL_UPLOAD_MAX_BYTES),
        },
        status=HTTPStatus.BAD_REQUEST,
    )


@require_http_methods(["POST"])
@login_required
@require_can(VAULT_CAPABILITY)
def request_upload(request: PortalVaultRequest, pk: UUID) -> HttpResponse:
    """Accept one file against one of the client's open requests (S7.5).

    The tenant, client and uploader are the middleware's verified context; nothing in
    the POST body or the file name selects them. A foreign or closed request id is
    404. Over 10 MiB is 400 with a pt-BR sentence, before anything is stored.
    """
    _require_flag(request)
    tenant = request.tenant
    if tenant is None:
        raise Http404
    limit = _size_label(settings.PORTAL_UPLOAD_MAX_BYTES)
    try:
        result = intake_upload(
            request.user,
            pk,
            request.FILES.get("file"),
            tenant=tenant,
            client=request.client,
        )
    except UploadTooLarge:
        return _refused(
            request,
            pk,
            f"O arquivo passa do limite de {limit}. Envie um arquivo de até {limit}.",
        )
    except UploadMissing:
        return _refused(request, pk, "Escolha um arquivo antes de enviar.")
    notice = (
        "Este arquivo já tinha sido enviado para esta solicitação. Nada mudou."
        if result.already_linked
        else "Você já tinha enviado este mesmo arquivo antes. Ele foi vinculado a "
        "esta solicitação, sem cópia nova."
        if result.relinked
        else "Arquivo recebido. A equipe ainda vai conferir."
    )
    if request.headers.get(HX_REQUEST):
        evidence_request = _visible_request(pk)
        return render(
            request,
            "portal/partials/request_status.html",
            {**_status_context(evidence_request), "notice": notice},
        )
    messages.success(request, notice)
    return redirect("portal-request-detail", pk=pk)


@require_http_methods(["POST"])
@login_required
def company_select(request: PortalVaultRequest) -> HttpResponse:
    """Choose which of the account's companies the portal shows (T11, S15 J).

    Runs UNCONFINED (`COMPANY_SELECT_PREFIX`), as app_runtime with both GUCs empty,
    because rotating the session key writes django_session. It reads no client-scoped
    row. The chosen client must be one this account holds an ACTIVE seat in, at this
    firm, re-checked here; otherwise 403 and the session is not touched. Middleware
    re-verifies the choice on every later request, so a seat revoked after this POST
    stops being honoured at once.
    """
    slug = portal_slug_from_host(request.get_host())
    # PLATFORM_QUERY_OK: the bootstrap lookup PortalMiddleware makes, by host slug.
    tenant = Tenant.objects.filter(slug=slug, is_active=True).first()
    if tenant is None or not tenant_row_has(tenant, FLAG):
        raise Http404
    try:
        client_id = UUID(str(request.POST.get("client", "")))
    except ValueError:
        raise PermissionDenied from None
    # PLATFORM_QUERY_OK: tenants_membership is a tenancy root; confined to this user,
    # this firm and the one client being chosen.
    member = Membership.objects.filter(
        user=request.user,
        tenant=tenant,
        client_id=client_id,
        client__isnull=False,
        is_active=True,
    ).exists()
    if not member:
        msg = "This account holds no active seat in that company."
        raise PermissionDenied(msg)
    request.session[SELECTED_CLIENT_SESSION_KEY] = str(client_id)
    # A new session key for a new client context, so a fixated or observed key does
    # not carry over into it.
    request.session.cycle_key()
    return redirect("portal-home")


@require_http_methods(["GET"])
@login_required
def brand_stylesheet(request: PortalHttpRequest) -> HttpResponse:
    """Serve the firm's colour as a same-origin stylesheet (CSP bars inline styles)."""
    brand = getattr(request, "portal_brand", None)
    body = ""
    if brand is not None and brand.color:
        body = (
            f".portal-brand{{background-color:{brand.color};"
            f"color:{brand.foreground}}}\n"
        )
    response = HttpResponse(body, content_type="text/css; charset=utf-8")
    response["Cache-Control"] = "private, max-age=300"
    return response


@require_http_methods(["GET"])
@login_required
def brand_logo(request: PortalHttpRequest) -> HttpResponse:
    """Serve the firm's PNG logo through the app, never as a public storage URL."""
    brand = getattr(request, "portal_brand", None)
    if brand is None or not brand.logo_key:
        raise Http404
    try:
        with default_storage.open(brand.logo_key) as handle:
            payload = handle.read()
    except OSError as error:
        raise Http404 from error
    response = HttpResponse(payload, content_type="image/png")
    response["Cache-Control"] = "private, max-age=300"
    response["Content-Disposition"] = 'inline; filename="logo.png"'
    return response
