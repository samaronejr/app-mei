"""Celery tasks owned by the core app, and the two tenancy-aware task bases.

Workers run outside the request/response cycle, so `TenantMiddleware` never touches
them and `app.tenant_id` is never set. Under the fail-closed policy a sweep would then
see zero rows and report success — a nightly job that silently does nothing. These
bases close that hole by making the tenant an explicit part of the task contract.
"""

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any
from uuid import UUID

from celery import Task, shared_task
from django.db import connection

from apps.core.tenancy import MissingTenantContext, tenant_context

if TYPE_CHECKING:
    from apps.tenants.models import Tenant


@shared_task
def ping() -> str:
    """Return a constant so the broker-and-worker round trip can be asserted."""
    return "pong"


class TenantTask(Task):
    """A task that runs entirely inside one tenant's context.

    The tenant id is the first argument, positional or keyword. Invoking without one
    raises `MissingTenantContext` rather than quietly processing nothing.
    """

    abstract = True

    def __call__(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        """Enter the tenant's context before running the task body."""
        with tenant_context(self._tenant_id_from(args, kwargs)):
            return super().__call__(*args, **kwargs)

    @staticmethod
    def _tenant_id_from(args: tuple[Any, ...], kwargs: dict[str, Any]) -> UUID | None:
        raw = kwargs.get("tenant_id", args[0] if args else None)
        if raw is None or isinstance(raw, UUID):
            return raw
        if isinstance(raw, str):
            return UUID(raw)
        msg = f"tenant_id must be a UUID or its string form, got {type(raw).__name__}."
        raise MissingTenantContext(msg)


def assert_no_transaction() -> None:
    """Refuse external I/O while any enclosing transaction could roll back."""
    if connection.in_atomic_block:
        msg = "External I/O is forbidden inside a database transaction."
        raise RuntimeError(msg)


class PhasedTenantTask(TenantTask):
    """Validate the firm without wrapping the body; each DB phase owns a context."""

    abstract = True

    def __call__(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        """Fail closed on missing, malformed or inactive tenants before work."""
        from apps.tenants.models import Tenant  # noqa: PLC0415

        try:
            tenant_id = self._tenant_id_from(args, kwargs)
        except ValueError as error:
            msg = "Invalid tenant UUID."
            raise MissingTenantContext(msg) from error
        active = tenant_id is not None and (
            # PLATFORM_QUERY_OK: validate exactly the task's explicit tenant, not data.
            Tenant.objects.filter(pk=tenant_id, is_active=True).exists()
        )
        if not active:
            msg = "An active tenant is required."
            raise MissingTenantContext(msg)
        assert_no_transaction()
        return Task.__call__(self, *args, **kwargs)


class PlatformTask(Task):
    """A task that legitimately spans tenants, and must visit them one at a time.

    It establishes no context of its own. Cross-tenant work iterates `each_tenant()`,
    which enters and leaves one tenant's context per step, so no query is ever made
    with two tenants in scope.
    """

    abstract = True

    @staticmethod
    def tenant_ids(*, include_inactive: bool = False) -> list[UUID]:
        """Collect firms without holding a transaction across external I/O."""
        from apps.tenants.models import Tenant  # noqa: PLC0415

        # PLATFORM_QUERY_OK: explicit enumeration for phased platform dispatch.
        queryset = Tenant.objects.all()
        if not include_inactive:
            queryset = queryset.filter(is_active=True)
        return list(queryset.values_list("pk", flat=True))

    @staticmethod
    def each_tenant(*, include_inactive: bool = False) -> Iterator["Tenant"]:
        """Yield every tenant with that tenant's context already established."""
        from apps.tenants.models import Tenant  # noqa: PLC0415

        # PLATFORM_QUERY_OK: enumerating firms is the declared purpose of a
        # PlatformTask. Each one's rows are then reached only inside that tenant's
        # own context, so no query ever runs with two tenants in scope.
        queryset = Tenant.objects.all()
        if not include_inactive:
            queryset = queryset.filter(is_active=True)
        for tenant in queryset.order_by("slug"):
            with tenant_context(tenant.id):
                yield tenant
