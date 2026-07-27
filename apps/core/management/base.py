"""Base command for management commands that operate on exactly one tenant."""

from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.core.tenancy import tenant_context
from apps.tenants.models import Tenant


class TenantAwareBaseCommand(BaseCommand):
    """Require `--tenant` and run `handle_tenant` inside that tenant's context.

    A management command runs outside the request cycle, so nothing establishes
    `app.tenant_id` for it. Without this base, a command would read zero rows and
    report success.
    """

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Register the mandatory tenant selector."""
        # dest is not "tenant": handle_tenant() receives the resolved Tenant under
        # that name, and sharing the key would collide with the raw slug.
        parser.add_argument(
            "--tenant",
            dest="tenant_slug",
            required=True,
            help="Slug of the tenant to operate on.",
        )

    def handle(self, *args: Any, **options: Any) -> str | None:  # noqa: ANN401
        """Resolve the tenant, then delegate inside its context."""
        slug = options["tenant_slug"]
        tenant = Tenant.objects.filter(slug=slug, is_active=True).first()
        if tenant is None:
            msg = f"No active tenant with slug {slug!r}."
            raise CommandError(msg)
        with tenant_context(tenant.id):
            return self.handle_tenant(tenant, *args, **options)

    def handle_tenant(self, tenant: Tenant, *args: Any, **options: Any) -> str | None:  # noqa: ANN401
        """Do the tenant-scoped work. Subclasses must override."""
        raise NotImplementedError
