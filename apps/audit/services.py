"""Writing audit records, and getting the transaction boundary right.

The subtle half of this module is `record_platform_event`. `ATOMIC_REQUESTS` makes the
view callable a **savepoint**, so an audit row written by a view that then raises is
rolled back with the rest of it — and the records most worth keeping are exactly the
ones written by requests that go on to fail: a denied action, a rejected credential.

So platform events emitted during a request are buffered on the request and flushed by
`PlatformEventMiddleware`, which is registered **outside** `TenantMiddleware` and
therefore runs in autocommit after the tenant transaction has already resolved. The
row survives the rollback that erases everything else the request touched.

Outside a request — Celery, management commands, tests — there is no buffer and the
write happens immediately.
"""

from contextvars import ContextVar, Token
from dataclasses import dataclass
from uuid import UUID

from django.contrib.auth.models import AnonymousUser

from apps.accounts.models import User
from apps.audit.models import AuditAction, Event, PlatformEvent
from apps.core.tenancy import MissingTenantContext, current_tenant_id, tenant_context

_pending_platform_events: ContextVar[list[PlatformEvent] | None] = ContextVar(
    "pending_platform_events",
    default=None,
)


@dataclass(frozen=True, slots=True)
class ObjectRef:
    """What an event refers to, as a reference rather than as a copy.

    The audit trail is append-only and LGPD grants erasure rights, so a CPF or a
    company name written into an event would become an unerasable personal-data
    store. Events therefore name a type and an id and nothing else.
    """

    type: str = ""
    id: str = ""


@dataclass(frozen=True, slots=True)
class Origin:
    """Who initiated an identity event, and from where.

    `subject` is text rather than a foreign key because the interesting case — a
    rejected credential — names an address with no account behind it.
    """

    actor: User | AnonymousUser | None = None
    subject: str = ""
    ip: str | None = None


def record_event(
    *,
    action: str,
    tenant_id: UUID | None = None,
    actor: User | None = None,
    obj: ObjectRef | None = None,
    metadata: dict[str, object] | None = None,
) -> Event:
    """Write a tenant-attributable event, entering tenant context if needed.

    Raises rather than falling back to a platform record when no tenant is known: an
    action classified as tenant-attributable but written without a tenant would be an
    audit row nobody can answer "whose data was this?" about.
    """
    resolved = tenant_id or current_tenant_id.get()
    if resolved is None:
        msg = (
            f"record_event(action={action!r}) needs a tenant. Tenant-attributable "
            f"actions are written inside tenant_context; identity events that happen "
            f"before tenant resolution belong in record_platform_event."
        )
        raise MissingTenantContext(msg)

    reference = obj or ObjectRef()
    fields = {
        "tenant_id": resolved,
        "actor": actor,
        "action": action,
        "object_type": reference.type,
        "object_id": reference.id,
        "metadata": metadata or {},
    }
    if current_tenant_id.get() == resolved:
        return Event.objects.create(**fields)
    with tenant_context(resolved):
        return Event.objects.create(**fields)


def record_platform_event(
    *,
    action: str,
    origin: Origin | None = None,
    tenant_id: UUID | None = None,
    metadata: dict[str, object] | None = None,
) -> PlatformEvent:
    """Write an identity event that may precede, or never have, a tenant."""
    source = origin or Origin()
    event = PlatformEvent(
        tenant_id=tenant_id,
        actor=source.actor if isinstance(source.actor, User) else None,
        action=action,
        subject=source.subject,
        ip=source.ip,
        metadata=metadata or {},
    )
    buffer = _pending_platform_events.get()
    if buffer is None:
        event.save()
        return event
    buffer.append(event)
    return event


PlatformEventToken = Token[list[PlatformEvent] | None]


def open_platform_event_buffer() -> PlatformEventToken:
    """Start buffering platform events for the current request."""
    return _pending_platform_events.set([])


def flush_platform_events(token: PlatformEventToken) -> None:
    """Write every buffered event, then drop the buffer.

    Runs outside the tenant transaction, which is the entire point: a request that
    raised has had its writes rolled back by then, and these rows must not be.
    """
    buffer = _pending_platform_events.get()
    try:
        if buffer:
            # PLATFORM_QUERY_OK: a write of rows this request itself constructed.
            PlatformEvent.objects.bulk_create(buffer)
    finally:
        _pending_platform_events.reset(token)


__all__ = [
    "AuditAction",
    "ObjectRef",
    "Origin",
    "PlatformEventToken",
    "flush_platform_events",
    "open_platform_event_buffer",
    "record_event",
    "record_platform_event",
]
