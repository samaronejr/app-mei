"""App configuration for the shared core package."""

from django.apps import AppConfig
from django.core.checks import register
from django.db.models.signals import post_migrate

from apps.core.checks import (
    check_email_config,
    check_object_storage_config,
    check_portal_capability_gates,
    check_portal_grant_drift,
    check_portal_role,
    check_tenant_middleware,
    check_tenant_slugs,
    check_transaction_and_cookie_policy,
)
from apps.core.grants import apply_portal_grants


class CoreConfig(AppConfig):
    """Register the deployment-invariant settings guards on startup."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"

    def ready(self) -> None:
        """Attach the settings guards and the portal-grant healer.

        `sender=self` binds the healer to this AppConfig alone, so it fires ONCE per
        `migrate` invocation rather than once for each installed app. It also fires on
        every `TransactionTestCase` flush teardown -- roughly 470 times per suite --
        because `flush` emits `post_migrate` too, which is why `apply_portal_grants`
        memoises its roles.sql parse and issues no DDL when nothing is missing.
        """
        post_migrate.connect(
            apply_portal_grants,
            sender=self,
            dispatch_uid="core.apply_portal_grants",
        )
        register(check_transaction_and_cookie_policy)
        register(check_tenant_middleware)
        register(check_portal_role)
        register(check_portal_capability_gates)
        register(check_object_storage_config)
        register(check_email_config)
        register(check_portal_grant_drift)
        register(check_tenant_slugs)
