"""Flush buffered platform events outside the tenant transaction.

Registered BEFORE `TenantMiddleware`, so by the time this middleware's `finally` runs,
that transaction has already committed or rolled back and this write lands in
autocommit. That ordering is the whole mechanism: a login failure or a denied action
is recorded by a request whose own writes were rolled back, and the audit row has to
outlive them.
"""

from collections.abc import Callable

from django.http import HttpRequest
from django.http.response import HttpResponseBase

from apps.audit.services import flush_platform_events, open_platform_event_buffer


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
