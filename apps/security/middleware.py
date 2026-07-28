"""Apply the rate-limit policy to every request.

Registered AFTER `TenantMiddleware`, because the authenticated limits are keyed on
`(tenant_id, user_id)` and the tenant is not resolved before then. The credential
limits do not need it — and must not use it — but running both from one place keeps
the policy in a single readable module rather than split across the stack.
"""

from collections.abc import Callable
from http import HTTPStatus

from django.http import HttpRequest, HttpResponse
from django.http.response import HttpResponseBase
from django.utils.translation import gettext as _

from apps.security.ratelimit import (
    authenticated_limit,
    credential_limits,
    exceeds,
    is_credential_endpoint,
)


class RateLimitMiddleware:
    """Reject requests that have exhausted their bucket, with 429 and a pt-BR body."""

    def __init__(
        self,
        get_response: Callable[[HttpRequest], HttpResponseBase],
    ) -> None:
        """Store the next callable in the middleware chain."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        """Count the request, and refuse it when a bucket has overflowed."""
        if self._is_limited(request):
            return self._too_many()
        return self.get_response(request)

    @staticmethod
    def _is_limited(request: HttpRequest) -> bool:
        if is_credential_endpoint(request):
            # Both buckets are counted, not short-circuited: an attacker spreading
            # attempts across many addresses must still exhaust the per-IP bucket.
            overflowed = [exceeds(request, limit) for limit in credential_limits()]
            return any(overflowed)
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        return exceeds(request, authenticated_limit(request))

    @staticmethod
    def _too_many() -> HttpResponse:
        return HttpResponse(
            _("Muitas requisições. Tente novamente em instantes."),
            status=HTTPStatus.TOO_MANY_REQUESTS,
            content_type="text/plain; charset=utf-8",
        )
