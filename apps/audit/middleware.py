"""Flush buffered platform events outside the tenant transaction.

Registered BEFORE `TenantMiddleware`, so by the time this middleware's `finally` runs,
that transaction has already committed or rolled back and this write lands in
autocommit. That ordering is the whole mechanism: a login failure or a denied action
is recorded by a request whose own writes were rolled back, and the audit row has to
outlive them.
"""

from collections.abc import Callable
from typing import Any, Protocol, cast
from uuid import UUID

from django.conf import settings
from django.http import HttpRequest
from django.http.response import HttpResponseBase
from django.urls import Resolver404, resolve

from apps.accounts.models import User
from apps.audit.models import AccessLog, AuditAction
from apps.audit.services import (
    ObjectRef,
    Origin,
    flush_platform_events,
    open_platform_event_buffer,
    record_event,
    record_platform_event,
)
from apps.core.netaddr import client_ip


class _SupportGrant(Protocol):
    """The two attributes the audit write reads off a live grant row."""

    tenant_id: UUID
    pk: UUID


UA_MAX_LENGTH = 1024
PATH_MAX_LENGTH = 2048

REDACTION_MARKER = "<redacted>"
RESET_FROM_KEY_URL_NAME = "account_reset_password_from_key"
RESET_FORM_URL_KEY = "set-password"


def _replace_path_segment(path: str, credential: str, replacement: str) -> str:
    """Replace one exact path segment and leave every other byte untouched."""
    segments = path.split("/")
    try:
        index = segments.index(credential)
    except ValueError:
        return path
    segments[index] = replacement
    return "/".join(segments)


def _recorded_path(request: HttpRequest) -> str:
    """Return the full path with registered URL-path credentials replaced.

    The query string is split off FIRST and reattached untouched. `get_full_path()` is
    path and query together, so rewriting the joined string could swallow the query.
    Registry membership is the gate: adding a credential route and enabling its
    redaction are one act, rather than two prefix lists that can drift apart.
    """
    full_path = request.get_full_path()
    path, separator, query = full_path.partition("?")
    try:
        match = resolve(
            request.path_info,
            urlconf=getattr(request, "urlconf", None),
        )
    except Resolver404:
        return full_path
    if match.url_name not in settings.CREDENTIAL_BEARING_URL_NAMES:
        return full_path

    parameters = {name: str(value) for name, value in match.kwargs.items()}
    if match.url_name == RESET_FROM_KEY_URL_NAME:
        uidb36 = parameters.get("uidb36")
        key = parameters.get("key")
        if key == RESET_FORM_URL_KEY:
            return full_path
        if uidb36 is not None and key is not None:
            path = _replace_path_segment(
                path,
                f"{uidb36}-{key}",
                f"{uidb36}-{REDACTION_MARKER}",
            )
    else:
        for credential in parameters.values():
            path = _replace_path_segment(path, credential, REDACTION_MARKER)
    return f"{path}{separator}{query}"


class PlatformEventMiddleware:
    """Collect platform events during a request and write them after it."""

    def __init__(
        self,
        get_response: Callable[[HttpRequest], HttpResponseBase],
    ) -> None:
        """Store the next callable in the middleware chain."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        """Buffer events for the duration of the request, then flush them."""
        token = open_platform_event_buffer()
        try:
            return self.get_response(request)
        finally:
            flush_platform_events(token)


class AccessLogMiddleware:
    """Record every request for the Marco Civil six-month access log.

    Registered BEFORE `TenantMiddleware`, and that ordering is the control rather than
    a preference. Inside that middleware's transaction, the rollback on a failed
    request would erase the access record for exactly the request an investigation is
    looking for — a statutory obligation defeated by a transaction boundary. Outside
    it, the write lands in autocommit and survives.

    `request.tenant` is read defensively: `TenantMiddleware` runs further in and may
    refuse the request with 403 before setting the attribute at all.
    """

    def __init__(
        self,
        get_response: Callable[[HttpRequest], HttpResponseBase],
    ) -> None:
        """Store the next callable in the middleware chain."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        """Serve the request, then record it unless it is infrastructure traffic."""
        response = self.get_response(request)
        if not self._is_exempt(request.path_info):
            self._record(request, response.status_code)
        return response

    @staticmethod
    def _is_exempt(path: str) -> bool:
        """Report whether a path is outside the Marco Civil access-record duty.

        The liveness probe and static assets are not a person reaching personal
        data — they are infrastructure. Logging the probe is also actively harmful:
        it is declared `non_atomic_requests` precisely so a database blip cannot make
        an orchestrator restart a healthy process, and a write here would reintroduce
        the database dependency that decorator exists to remove.
        """
        return path.startswith(tuple(settings.ACCESS_LOG_EXEMPT_PREFIXES))

    @staticmethod
    def _record(request: HttpRequest, status_code: int) -> None:
        tenant = getattr(request, "tenant", None)
        user = getattr(request, "user", None)
        # PLATFORM_QUERY_OK: a write describing this request and nothing else.
        AccessLog.objects.create(
            tenant=tenant,
            user=user if isinstance(user, User) else None,
            ip=client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:UA_MAX_LENGTH],
            method=request.method or "",
            path=_recorded_path(request)[:PATH_MAX_LENGTH],
            status_code=status_code,
        )


class SupportRequestAuditMiddleware:
    """Dual-write every request answered under a live support grant (AXR1-023 B2).

    A support session's session_started row says a door opened; it says nothing
    about what was touched inside. Every request served with
    ``request.support_grant`` set — GETs included — is therefore written to BOTH
    audit streams: a PlatformEvent for the platform-side actor and a tenant
    ``Event`` the owner's ``/configuracoes/suporte/`` page renders.

    Registered BEFORE `TenantMiddleware` for the same reason AccessLog is: the
    write lands after that transaction resolves, so a denied or failed request
    is still recorded — exactly the request an investigation wants. Requests
    that never set ``request.support_grant`` (no grant, static, health probes)
    cost one attribute check and nothing else.
    """

    def __init__(
        self,
        get_response: Callable[[HttpRequest], HttpResponseBase],
    ) -> None:
        """Store the next callable in the middleware chain."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        """Serve the request, then dual-write it when a grant was attached."""
        try:
            response = self.get_response(request)
        except Exception:
            self._audit(request, 500)
            raise
        self._audit(request, response.status_code)
        return response

    def _audit(self, request: HttpRequest, status_code: int) -> None:
        """Dual-write when a grant rode the request; exempts stay quiet."""
        grant = getattr(request, "support_grant", None)
        if grant is not None and not self._is_exempt(request.path_info):
            self._record(request, grant, status_code)

    @staticmethod
    def _is_exempt(path: str) -> bool:
        """Skip infrastructure traffic — the same set the access log skips."""
        return path.startswith(tuple(settings.ACCESS_LOG_EXEMPT_PREFIXES))

    @staticmethod
    def _record(request: HttpRequest, grant: _SupportGrant, status_code: int) -> None:
        """Write one row into each audit stream for this request."""
        grant = cast("Any", request).support_grant
        metadata: dict[str, object] = {
            "method": request.method or "",
            "path": request.path_info[:PATH_MAX_LENGTH],
            "status_code": status_code,
        }
        user = getattr(request, "user", None)
        actor = user if isinstance(user, User) else None
        # The platform stream buffers and flushes in PlatformEventMiddleware —
        # the same write a rolled-back request cannot take with it.
        record_platform_event(
            action=AuditAction.SUPPORT_REQUEST,
            tenant_id=grant.tenant_id,
            origin=Origin(actor=actor, ip=client_ip(request)),
            metadata={"grant_id": str(grant.pk), **metadata},
        )
        # The tenant stream opens its own context: the request transaction has
        # already resolved by the time this outer finally runs, so the write is
        # its own committed row — two queries, no N+1.
        record_event(
            action=AuditAction.SUPPORT_REQUEST,
            tenant_id=grant.tenant_id,
            actor=actor,
            obj=ObjectRef(type="SupportGrant", id=str(grant.pk)),
            metadata=metadata,
        )
