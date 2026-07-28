"""Flush buffered platform events outside the tenant transaction.

Registered BEFORE `TenantMiddleware`, so by the time this middleware's `finally` runs,
that transaction has already committed or rolled back and this write lands in
autocommit. That ordering is the whole mechanism: a login failure or a denied action
is recorded by a request whose own writes were rolled back, and the audit row has to
outlive them.
"""

from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest
from django.http.response import HttpResponseBase

from apps.accounts.models import User
from apps.audit.models import AccessLog
from apps.audit.services import flush_platform_events, open_platform_event_buffer
from apps.core.netaddr import client_ip

UA_MAX_LENGTH = 1024
PATH_MAX_LENGTH = 2048


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
            path=request.get_full_path()[:PATH_MAX_LENGTH],
            status_code=status_code,
        )
