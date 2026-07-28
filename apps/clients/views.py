"""Client registry views. Currently one: the CSV export.

The export is gated on `clients.view_all` rather than `clients.view_assigned`. A file
containing the whole registry is exactly what the `limited` level exists to withhold,
and a staff accountant restricted to their own assignments must not be able to obtain
every client's CNPJ by changing the URL.
"""

from django.http import HttpRequest, HttpResponse
from django.views.decorators.http import require_http_methods

from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import record_event
from apps.authz.services import require_can
from apps.clients.exports import UTF8_BOM, build_client_csv
from apps.clients.models import ClientCompany
from apps.core.tenancy import current_tenant_id

EXPORT_FILENAME = "clientes.csv"


@require_http_methods(["GET"])
@require_can("clients.view_all")
def export_clients_csv(request: HttpRequest) -> HttpResponse:
    """Return the firm's client registry as a CSV attachment.

    **Materialized with `list()` before a single byte is written, and returned as a
    plain `HttpResponse`.** Returning a `StreamingHttpResponse` here would raise
    `TypeError` in `TenantMiddleware`, and that guard exists because the alternative
    is worse than an error: the iterator would run after the tenant transaction had
    closed, every query inside it would return zero rows under the fail-closed policy,
    and the accountant would receive an empty file that downloaded successfully.
    """
    companies = list(ClientCompany.objects.all().order_by("legal_name"))
    payload = build_client_csv(companies)

    tenant_id = current_tenant_id.get()
    if tenant_id is not None:
        # Bulk export of personal data is precisely the action LGPD art. 37 expects to
        # find in the record of processing operations, so it is audited even though
        # nothing was modified.
        record_event(
            action=AuditAction.EXPORT,
            tenant_id=tenant_id,
            actor=request.user if isinstance(request.user, User) else None,
            metadata={"kind": "clients_csv", "rows": len(companies)},
        )

    # The byte-order mark is what stops Excel reading a UTF-8 file as the system
    # codepage and turning every accented legal name into mojibake.
    response = HttpResponse(
        (UTF8_BOM + payload).encode("utf-8"),
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = f'attachment; filename="{EXPORT_FILENAME}"'
    return response
