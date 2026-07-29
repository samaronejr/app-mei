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

The portal adds a second dimension on the same pattern: `current_client_id` and the
`app.client_id` GUC, which drive the RESTRICTIVE policies confining a portal session to
one client of the firm. Tenant scope alone cannot express that, because a portal user
sits *inside* the accounting firm's tenant alongside every other client.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

from django.db import connection, transaction

TENANT_GUC = "app.tenant_id"
CLIENT_GUC = "app.client_id"
NO_TENANT = ""
NO_CLIENT = ""

current_tenant_id: ContextVar[UUID | None] = ContextVar(
    "current_tenant_id",
    default=None,
)

current_client_id: ContextVar[UUID | None] = ContextVar(
    "current_client_id",
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


class MissingClientContext(RuntimeError):  # noqa: N818
    """Raised when client-scoped work is attempted with no client established.

    Same reasoning as `MissingTenantContext`, one dimension down: the portal's
    restrictive policies compare against NULL when `app.client_id` is unset, so the
    quiet alternative is again zero rows rather than an error.
    """


def _read_guc(name: str = TENANT_GUC, missing: str = NO_TENANT) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT coalesce(current_setting(%s, true), '')", [name])
        row = cursor.fetchone()
    return str(row[0]) if row else missing


def _write_guc(value: str, name: str = TENANT_GUC) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, %s, true)", [name, value])


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


@contextmanager
def client_context(client_id: UUID | None) -> Iterator[UUID]:
    """Establish the client dimension for a block of work, then restore it.

    This sets `app.client_id` and nothing else. On its own it confines NOTHING at the
    database layer: the portal policies are RESTRICTIVE **to `app_portal`**, and
    `app_runtime` does not inherit that role — the grant is `WITH INHERIT FALSE`
    precisely so those policies do not bind the firm's own connection. Confinement
    therefore requires `SET LOCAL ROLE app_portal` as well, which the portal middleware
    issues. Until then this is an application-layer signal that `role_of` and
    `_is_attached_to` read, not an isolation boundary.

    It mirrors `tenant_context` otherwise — including restoring the previous value
    explicitly rather than in a `finally`, for the savepoint-merge reason documented
    there.

    The tenant dimension is NOT set here. A client always sits inside a tenant, so
    callers establish both, and keeping them separate lets the middleware set the
    tenant while a portal user is still anonymous and the client is not yet known.
    """
    if client_id is None:
        msg = (
            "client_context requires a client id. Refusing to run client-scoped work "
            "with no context, which would silently touch zero rows."
        )
        raise MissingClientContext(msg)

    token = current_client_id.set(client_id)
    try:
        with transaction.atomic():
            previous = _read_guc(CLIENT_GUC, NO_CLIENT)
            _write_guc(str(client_id), CLIENT_GUC)
            yield client_id
            _write_guc(previous, CLIENT_GUC)
    finally:
        current_client_id.reset(token)
