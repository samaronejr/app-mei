"""Keep the R1 client journey in pt-BR without changing legacy negotiation."""

from django.http import HttpRequest
from django.middleware.locale import LocaleMiddleware
from django.utils import translation

from apps.core.flags import tenant_row_has
from apps.portal.hosts import portal_slug_from_host
from apps.tenants.middleware import TenantMiddleware
from apps.tenants.models import Tenant


class ClientLocaleMiddleware(LocaleMiddleware):
    """Select the client locale before authentication, MFA and portal confinement."""

    def process_request(self, request: HttpRequest) -> None:
        """Negotiate normally except on an active R1 firm's portal host."""
        super().process_request(request)
        if TenantMiddleware.is_non_atomic(request):
            return
        slug = portal_slug_from_host(request.get_host())
        if slug is None:
            return
        # PLATFORM_QUERY_OK: like portal routing, this bootstrap read precedes
        # authentication and SET LOCAL ROLE; no client identity comes from the host.
        tenant = Tenant.objects.filter(slug=slug, is_active=True).first()
        if tenant is not None and tenant_row_has(tenant, "cycles_r1"):
            translation.activate("pt-br")
            request.LANGUAGE_CODE = translation.get_language()
