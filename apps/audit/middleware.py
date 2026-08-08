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

# Path prefixes whose NEXT segment is a bearer credential rather than an identifier.
# Distinct from `ACCESS_LOG_EXEMPT_PREFIXES` and deliberately not merged with it:
# exemption drops the record, which is the Marco Civil duty being abandoned, while this
# keeps the visit and drops only the secret. An invitation token stays valid for seven
# days and this table keeps rows for a hundred and eighty, so a path written verbatim
# would outlive the credential by half a year in the one table nothing is allowed to
# delete from — and every downstream copy of it.
#
# Both hosts are covered by this single entry because both acceptance routes are mounted
# on the same path: firm-side at `apps/accounts/urls.py`, portal-side at
# `apps/portal/urls.py`. A prefix per host would be two things to keep in step.
REDACTED_PATH_PREFIXES: tuple[str, ...] = ("/convites/aceitar/",)
REDACTION_MARKER = "<redacted>"


def _recorded_path(request: HttpRequest) -> str:
    """Return the request's full path with any credential segment replaced.

    The query string is split off FIRST and reattached untouched. `get_full_path()` is
    path and query together, the credential is a path segment, and a rewrite applied to
    the joined string would either swallow the query or stop at the wrong separator
    depending on which end it cut from. Only the one segment immediately after a matched
    prefix is replaced; everything deeper survives, so a longer path stays as legible as
    a bare one.
    """
    full_path = request.get_full_path()
    path, separator, query = full_path.partition("?")
    for prefix in REDACTED_PATH_PREFIXES:
        if path.startswith(prefix):
            _credential, slash, tail = path[len(prefix) :].partition("/")
            path = f"{prefix}{REDACTION_MARKER}{slash}{tail}"
            break
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
