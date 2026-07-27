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

from contextvars import ContextVar
from uuid import UUID

current_tenant_id: ContextVar[UUID | None] = ContextVar(
    "current_tenant_id",
    default=None,
)
