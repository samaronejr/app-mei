"""Owner-only account lifecycle, estimates, exports and existing legal holds."""

from http import HTTPStatus
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts.views import AuthenticatedRequest
from apps.authz.firm_account import ACCOUNT_CAPABILITY, require_account_owner
from apps.authz.services import require_can
from apps.clients.forms import VersionForm
from apps.clients.maintenance import load_client
from apps.clients.models import ClientCompany
from apps.clients.views import require_tenant
from apps.core.flags import require_feature
from apps.core.retention import RetentionClass
from apps.cycles.archive_response import ArchiveDescriptor, EvidenceArchiveResponse
from apps.cycles.errors import IllegalTransition, VersionConflict
from apps.cycles.retention import place_hold, release_hold
from apps.cycles.retention_records import LegalHold
from apps.tenants.account_state import export_available, export_until, read_only_reason
from apps.tenants.entitlements import client_allowance
from apps.tenants.export_service import request_export
from apps.tenants.forms import CancellationForm, HoldForm
from apps.tenants.lifecycle import schedule_cancellation
from apps.tenants.models import AccountExport, AccountExportState, BillingAccount
from apps.tenants.services import TenantConflictError
from apps.tenants.usage import cost_estimate
from apps.tenants.views import FLAG, resolved_firm


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(ACCOUNT_CAPABILITY)
def account_view(request: AuthenticatedRequest) -> HttpResponse:
    """Present manual billing and estimates only after checking a live owner seat."""
    tenant = resolved_firm(request)
    require_account_owner(request.user, tenant.pk)
    form = CancellationForm(
        request.POST or None,
        initial={
            "version": tenant.settings_version,
            "ends_on": (
                tenant.cancellation_ends_on.isoformat()
                if tenant.cancellation_ends_on is not None
                else ""
            ),
        },
    )
    status = HTTPStatus.OK
    if request.method == "POST":
        status = HTTPStatus.UNPROCESSABLE_ENTITY
        if form.is_valid():
            try:
                schedule_cancellation(
                    request.user,
                    tenant.pk,
                    ends_on=form.cleaned_data["ends_on"],
                    expected_version=form.cleaned_data["version"],
                )
            except TenantConflictError as error:
                form.add_error(None, str(error))
                status = HTTPStatus.CONFLICT
            except ValidationError as error:
                form.add_error(None, " ".join(error.messages))
            else:
                messages.success(request, "Encerramento agendado.")
                return HttpResponseRedirect(request.path)
    return render(
        request,
        "tenants/account.html",
        {
            "firm": tenant,
            "billing": BillingAccount.objects.first(),
            "form": form,
            "allowance": client_allowance(tenant),
            "estimate": cost_estimate(request.user, tenant.pk, on=timezone.localdate()),
            "exports": AccountExport.objects.order_by("-created_at")[:20],
            "may_export": export_available(tenant),
            "export_deadline": export_until(tenant),
            "read_only": read_only_reason(tenant),
            "retention_classes": RetentionClass.choices,
            "purge_dry_run": settings.RETENTION_PURGE_DRY_RUN,
            # CLIENT_SCOPE_OK: the verified owner reviews the firm's retention roster.
            "clients": Paginator(
                ClientCompany.objects.order_by("legal_name", "pk"), 50
            ).get_page(request.GET.get("pagina", 1)),
        },
        status=status,
    )


@require_POST
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(ACCOUNT_CAPABILITY)
def export_request_view(request: AuthenticatedRequest) -> HttpResponse:
    """Export remains available during suspension and the cancellation grace window."""
    tenant = resolved_firm(request)
    require_account_owner(request.user, tenant.pk)
    form = VersionForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Recarregue a página antes de exportar.", status=409)
    try:
        export = request_export(
            request.user, tenant.pk, expected_version=form.cleaned_data["version"]
        )
    except TenantConflictError as error:
        return HttpResponse(str(error), status=HTTPStatus.CONFLICT)
    return HttpResponseRedirect(reverse("account-export-detail", args=[export.pk]))


@require_http_methods(["GET"])
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(ACCOUNT_CAPABILITY)
def export_detail_view(request: AuthenticatedRequest, export_id: UUID) -> HttpResponse:
    """Show the persisted job state without inventing a finished download."""
    tenant = resolved_firm(request)
    require_account_owner(request.user, tenant.pk)
    export = AccountExport.objects.filter(pk=export_id).first()
    if export is None:
        raise Http404
    return render(
        request,
        "tenants/export.html",
        {
            "export": export,
            "is_preparing": export.state
            in {AccountExportState.PENDING, AccountExportState.BUILDING},
            "ready": export.state == AccountExportState.READY
            and export_available(tenant)
            and export.expires_at is not None
            and export.expires_at > timezone.now(),
        },
    )


@require_http_methods(["GET", "HEAD"])
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(ACCOUNT_CAPABILITY)
def export_download_view(
    request: AuthenticatedRequest, export_id: UUID
) -> HttpResponse | EvidenceArchiveResponse:
    """Materialize only an authorized descriptor; byte I/O starts after commit."""
    tenant = resolved_firm(request)
    require_account_owner(request.user, tenant.pk)
    if not export_available(tenant):
        return HttpResponse("O prazo de exportação encerrou.", status=410)
    export = AccountExport.objects.filter(pk=export_id).first()
    if export is None:
        raise Http404
    if (
        export.state != AccountExportState.READY
        or export.expires_at is None
        or export.expires_at <= timezone.now()
    ):
        return HttpResponse("Este arquivo não está disponível.", status=410)
    return EvidenceArchiveResponse(
        ArchiveDescriptor(
            export.storage_key,
            f"conta-{export.pk}.zip",
            export.byte_size,
            export.sha256,
        )
    )


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(ACCOUNT_CAPABILITY)
def retention_view(request: AuthenticatedRequest, client_id: UUID) -> HttpResponse:
    """Reuse T18 hold commands, including live-purge and version checks."""
    tenant = resolved_firm(request)
    require_account_owner(request.user, tenant.pk)
    client = load_client(request.user, tenant.pk, client_id)
    form = HoldForm(request.POST or None, initial={"version": client.version})
    status = HTTPStatus.OK
    if request.method == "POST":
        status = HTTPStatus.UNPROCESSABLE_ENTITY
        if form.is_valid():
            try:
                place_hold(
                    request.user,
                    client_id,
                    reason=form.cleaned_data["reason"],
                    expected_version=form.cleaned_data["version"],
                )
            except (ValidationError, VersionConflict, IllegalTransition) as error:
                form.add_error(None, str(error))
                status = HTTPStatus.CONFLICT
            else:
                messages.success(request, "Retenção registrada.")
                return HttpResponseRedirect(request.path)
    return render(
        request,
        "tenants/retention.html",
        {
            "client": client,
            "form": form,
            "holds": LegalHold.objects.filter(client=client).order_by("-placed_at"),
            "read_only": read_only_reason(tenant),
        },
        status=status,
    )


@require_POST
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(ACCOUNT_CAPABILITY)
def release_hold_view(request: AuthenticatedRequest, hold_id: UUID) -> HttpResponse:
    """Release a reviewed hold without removing any retained row or evidence."""
    tenant = resolved_firm(request)
    require_account_owner(request.user, tenant.pk)
    form = VersionForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Recarregue a página antes de liberar.", status=409)
    try:
        hold = release_hold(
            request.user, hold_id, expected_version=form.cleaned_data["version"]
        )
    except (VersionConflict, IllegalTransition) as error:
        return HttpResponse(str(error), status=409)
    return HttpResponseRedirect(reverse("firm-retention", args=[hold.client_id]))
