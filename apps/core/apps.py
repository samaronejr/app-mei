"""App configuration for the shared core package."""

from django.apps import AppConfig
from django.core.checks import register

from apps.core.checks import (
    check_portal_capability_gates,
    check_portal_role,
    check_tenant_middleware,
    check_tenant_slugs,
    check_transaction_and_cookie_policy,
)


class CoreConfig(AppConfig):
    """Register the deployment-invariant settings guards on startup."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"

    def ready(self) -> None:
        """Attach the settings guards to Django's check framework."""
        register(check_transaction_and_cookie_policy)
        register(check_tenant_middleware)
        register(check_portal_role)
        register(check_portal_capability_gates)
        register(check_tenant_slugs)
