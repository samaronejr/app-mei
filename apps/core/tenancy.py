"""The ambient tenant of the current execution context.

Isolation in this product is enforced in two independent layers, and this module owns
the handle to the first one:

* **Layer 1, the ORM.** `TenantScopedManager` reads `current_tenant_id` and filters
  every queryset by it. This is what shapes ordinary application code.
* **Layer 2, the database.** The `app.tenant_id` GUC drives the row-level-security
  policies. This is what holds when layer 1 is bypassed, forgotten, or wrong.

Both must be set. Setting only the ContextVar leaves the database open; setting only
the GUC makes every scoped queryset silently empty. The middleware, the Celery task
base, and the management-command base all set both.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

from django.db import connection, transaction

TENANT_GUC = "app.tenant_id"
NO_TENANT = ""

current_tenant_id: ContextVar[UUID | None] = ContextVar(
    "current_tenant_id",
    default=None,
)


# Named without an Error suffix on purpose: this is the name the isolation design
# and its acceptance criteria refer to, and it reads correctly at the call site as
# `raise MissingTenantContext(...)`.
class MissingTenantContext(RuntimeError):  # noqa: N818
    """Raised when tenant-scoped work is attempted with no tenant established.

    This exists so the failure is loud. Under the fail-closed policy the quiet
    alternative is an empty result set, and a nightly sweep that processes zero rows
    and reports success is worse than one that crashes.
    """


def _read_guc() -> str:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT coalesce(current_setting('{TENANT_GUC}', true), '')")
        row = cursor.fetchone()
    return str(row[0]) if row else NO_TENANT


def _write_guc(value: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, %s, true)", [TENANT_GUC, value])


@contextmanager
def tenant_context(tenant_id: UUID | None) -> Iterator[UUID]:
    """Establish both isolation layers for a block of work, then restore both.

    Used by Celery tasks and management commands, which run outside the request cycle
    and would otherwise see zero rows under the fail-closed policy.
    """
    if tenant_id is None:
        msg = (
            "tenant_context requires a tenant id. Refusing to run tenant-scoped work "
            "with no context, which would silently touch zero rows."
        )
        raise MissingTenantContext(msg)

    token = current_tenant_id.set(tenant_id)
    try:
        with transaction.atomic():
            # Captured INSIDE the block so a nested use reads the enclosing
            # transaction's value rather than a stale snapshot.
            previous = _read_guc()
            _write_guc(str(tenant_id))
            yield tenant_id
            # Restored explicitly, and deliberately NOT in a `finally`.
            #
            # PostgreSQL MERGES a SET LOCAL into the parent transaction on RELEASE
            # SAVEPOINT rather than discarding it (verified on 16). So when this block
            # is entered while a transaction is already open — a PlatformTask looping
            # over tenants, an eager task inside a request — the value would survive
            # the block and the *next* iteration would start with the previous
            # tenant's context still in place.
            #
            # On the exception path this line is skipped on purpose: the savepoint
            # rolls back, which reverts the setting anyway, and issuing a statement on
            # an aborted transaction would raise and mask the original error.
            _write_guc(previous)
    finally:
        current_tenant_id.reset(token)
