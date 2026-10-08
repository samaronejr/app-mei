"""The portfolio import wizard (M2-I): upload, map, dry run, commit, roll back.

Behind the `import_r1` flag, resolved firm first (`require_tenant`), and gated on the
S8.1 `clients.import` capability — owner and operations admin — plus a firm-wide
portfolio (`importing.may_import`).

Nothing here streams. The error report is built in memory and returned as a plain
`HttpResponse`, because `TenantMiddleware` refuses a streaming response (the iterator
would run after the tenant transaction closed and read zero rows).
"""

from http import HTTPStatus
from typing import Any, Final
from uuid import UUID

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts.views import AuthenticatedRequest
from apps.authz.services import require_can
from apps.clients.exports import UTF8_BOM
from apps.clients.forms import ImportUploadForm, VersionForm, posted_mapping
from apps.clients.importing import (
    FIELDS,
    IMPORT_CAPABILITY,
    OUTCOME_LABELS,
    ImportConflictError,
    ImportFileError,
    MappingIncompleteError,
    RollbackRefusedError,
    build_error_csv,
    may_import,
    pending_client_count,
    plan_import,
    queue_commit,
    rollback_import,
)
from apps.clients.models import (
    ClientCompany,
    ImportBatch,
    ImportStatus,
    RowOutcome,
)
from apps.clients.tasks import commit_import_task
from apps.clients.views import require_tenant
from apps.clients.views_onboarding import FLAG
from apps.core.flags import require_feature
from apps.tenants.entitlements import EntitlementExceededError, client_allowance

# The rows the batch page lists inline; the full set is in the error report.
ROWS_ON_PAGE: Final = 200
STATUS_LABELS: Final[dict[str, Any]] = {
    ImportStatus.PLANNED: gettext_lazy("Prévia pronta"),
    ImportStatus.QUEUED: gettext_lazy("Importando"),
    ImportStatus.COMMITTED: gettext_lazy("Importado"),
    ImportStatus.PARTIAL: gettext_lazy("Importado com falhas"),
    ImportStatus.ROLLED_BACK: gettext_lazy("Desfeito"),
}


def _require_import(request: AuthenticatedRequest) -> UUID:
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404
    tenant_id = UUID(str(tenant.pk))
    if not may_import(request.user, tenant_id):
        msg = "Only firm-wide roles import clients."
        raise PermissionDenied(msg)
    return tenant_id


def _batch(batch_pk: UUID) -> ImportBatch:
    batch = ImportBatch.objects.filter(pk=batch_pk).first()
    if batch is None:
        raise Http404
    return batch


def _upload_page(
    request: AuthenticatedRequest,
    form: ImportUploadForm,
    *,
    status: int = HTTPStatus.OK,
    error: str = "",
    mapping_needed: bool = False,
) -> HttpResponse:
    return render(
        request,
        "clients/import_upload.html",
        {
            "form": form,
            "error": error,
            "mapping_needed": mapping_needed,
            "fields": FIELDS,
            "recent": ImportBatch.objects.order_by("-created_at", "pk")[:10],
            "status_labels": STATUS_LABELS,
            "page_title": _("Importar clientes"),
        },
        status=status,
    )


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(IMPORT_CAPABILITY)
def import_upload_view(request: AuthenticatedRequest) -> HttpResponse:
    """Run wizard steps 1-6: upload, map columns, and persist the dry run."""
    _require_import(request)
    if request.method == "GET":
        return _upload_page(request, ImportUploadForm())
    form = ImportUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        return _upload_page(request, form, status=HTTPStatus.UNPROCESSABLE_ENTITY)
    try:
        plan = plan_import(
            request.user,
            form.cleaned_data["file"],
            posted_mapping(request.POST),
        )
    except MappingIncompleteError as error:
        initial = {f"map_{key}": header for key, header in error.mapping.items()}
        return _upload_page(
            request,
            ImportUploadForm(headers=error.headers, initial=initial),
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
            error=error.message,
            mapping_needed=True,
        )
    except ImportFileError as error:
        return _upload_page(
            request,
            ImportUploadForm(),
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
            error=error.message,
        )
    return HttpResponseRedirect(reverse("client-import-batch", args=[plan.batch_id]))


def _batch_context(batch: ImportBatch, **extra: Any) -> dict[str, Any]:  # noqa: ANN401
    allowance = client_allowance(batch.tenant, additional=pending_client_count(batch))
    problem_rows = list(
        batch.rows.exclude(outcome=RowOutcome.CREATED, commit_error="").order_by(
            "row_number",
        )[:ROWS_ON_PAGE],
    )
    return {
        "batch": batch,
        "allowance": allowance,
        "status_label": STATUS_LABELS.get(batch.status, batch.status),
        "problem_rows": [
            {
                "row": row,
                "label": OUTCOME_LABELS.get(row.outcome, row.outcome),
                "reasons": [
                    *row.errors,
                    *([row.commit_error] if row.commit_error else []),
                ],
            }
            for row in problem_rows
        ],
        "rows_truncated": len(problem_rows) >= ROWS_ON_PAGE,
        "may_commit": batch.status in (ImportStatus.PLANNED, ImportStatus.PARTIAL)
        and (batch.planned_created > batch.committed_count)
        and allowance.allowed,
        "may_rollback": batch.status in (ImportStatus.COMMITTED, ImportStatus.PARTIAL),
        "in_progress": batch.status == ImportStatus.QUEUED,
        "is_planned": batch.status == ImportStatus.PLANNED,
        "is_partial": batch.status == ImportStatus.PARTIAL,
        "has_problems": bool(problem_rows),
        "page_title": _("Importação de clientes"),
        **extra,
    }


@require_http_methods(["GET"])
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(IMPORT_CAPABILITY)
def import_batch_view(request: AuthenticatedRequest, batch_pk: UUID) -> HttpResponse:
    """Show the dry-run result, the commit progress, or the committed batch."""
    _require_import(request)
    batch = _batch(batch_pk)
    return render(request, "clients/import_batch.html", _batch_context(batch))


def _conflict(request: AuthenticatedRequest, batch_pk: UUID) -> HttpResponse:
    return render(
        request,
        "clients/conflict.html",
        {
            "message": _(
                "Esta importação mudou desde que você abriu a página. Nada foi "
                "feito. Recarregue para ver a situação atual.",
            ),
            "back_url": reverse("client-import-batch", args=[batch_pk]),
        },
        status=HTTPStatus.CONFLICT,
    )


@require_POST
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(IMPORT_CAPABILITY)
def import_commit_view(request: AuthenticatedRequest, batch_pk: UUID) -> HttpResponse:
    """Step 7/8: queue the commit (or the retry of failed rows) as a TenantTask."""
    tenant_id = _require_import(request)
    form = VersionForm(request.POST)
    if not form.is_valid():
        return _conflict(request, batch_pk)
    try:
        batch = queue_commit(
            request.user,
            batch_pk,
            expected_version=form.cleaned_data["version"],
        )
    except ImportConflictError:
        return _conflict(request, batch_pk)
    except EntitlementExceededError as error:
        return render(
            request,
            "clients/import_batch.html",
            _batch_context(_batch(batch_pk), error=" ".join(error.messages)),
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    args = (str(tenant_id), str(batch.pk), str(request.user.pk), batch.version)
    # After commit, never inside the transaction: a worker that picked the task up
    # first would read the batch before it was queued and refuse it as stale.
    transaction.on_commit(lambda: commit_import_task.delay(*args))
    return HttpResponseRedirect(reverse("client-import-batch", args=[batch.pk]))


@require_POST
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(IMPORT_CAPABILITY)
def import_rollback_view(request: AuthenticatedRequest, batch_pk: UUID) -> HttpResponse:
    """Step 9: delete what the batch created, or explain what blocks it."""
    _require_import(request)
    form = VersionForm(request.POST)
    if not form.is_valid():
        return _conflict(request, batch_pk)
    try:
        rollback_import(
            request.user,
            batch_pk,
            expected_version=form.cleaned_data["version"],
        )
    except ImportConflictError:
        return _conflict(request, batch_pk)
    except RollbackRefusedError as refusal:
        batch = _batch(batch_pk)
        names = dict(
            ClientCompany.objects.filter(
                pk__in={dep.client_id for dep in refusal.dependents},
            ).values_list("pk", "legal_name"),
        )
        blocking = [
            {"dependent": dep, "client_name": names.get(dep.client_id, "")}
            for dep in refusal.dependents
        ]
        return render(
            request,
            "clients/import_batch.html",
            _batch_context(batch, blocking=blocking),
            status=HTTPStatus.CONFLICT,
        )
    return HttpResponseRedirect(reverse("client-import-batch", args=[batch_pk]))


@require_http_methods(["GET"])
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(IMPORT_CAPABILITY)
def import_errors_csv_view(
    request: AuthenticatedRequest,
    batch_pk: UUID,
) -> HttpResponse:
    """Download the rows that did not become clients, as a formula-defused CSV."""
    _require_import(request)
    batch = _batch(batch_pk)
    response = HttpResponse(
        (UTF8_BOM + build_error_csv(batch)).encode("utf-8"),
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = 'attachment; filename="erros-importacao.csv"'
    return response


__all__ = [
    "import_batch_view",
    "import_commit_view",
    "import_errors_csv_view",
    "import_rollback_view",
    "import_upload_view",
]
