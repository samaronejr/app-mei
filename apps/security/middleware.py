"""Two request-edge policies: the rate limit, and how an expired session answers HTMX.

**The rate limit** is applied on the way in. Registered AFTER `TenantMiddleware`,
because the authenticated limits are keyed on `(tenant_id, user_id)` and the tenant is
not resolved before then. The credential limits do not need it — and must not use it —
but running both from one place keeps the policy in a single readable module rather
than split across the stack.

**The login redirect is rewritten** on the way out, for HTMX requests only. The two
jobs share this module rather than a new middleware entry because the ordered group
`MFA -> HostDispatch -> Portal -> Tenant -> RateLimit` is pinned by `core.E005`,
`core.E006` and `core.E007`, which compare positions in `MIDDLEWARE`; a new entry is a
new position to get wrong, and the rewrite needs no position of its own. What it does
need is to be cheap, and it is: this middleware runs INSIDE the transaction
`PortalMiddleware` opens, under a role holding SELECT on seven tables, so the rewrite
issues zero queries — it reads a header, compares two paths, and returns.

Why rewrite at all: `login_required` answers an expired session with a 302, which is
right for a navigation and wrong for a swap. `XMLHttpRequest` follows a 3xx itself, so
HTMX never sees the redirect — it sees the login page at 200 and swaps the sign-in
form into whatever fragment was on screen, with no address-bar change to explain it.
`HX-Redirect` on a non-redirect status is the only shape that reaches HTMX intact, and
it makes the browser navigate for real.
"""

from collections.abc import Callable
from http import HTTPStatus
from urllib.parse import urlsplit

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.http.response import HttpResponseBase
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.security.ratelimit import (
    authenticated_limit,
    credential_limits,
    exceeds,
    is_public_post_endpoint,
)

# The header HTMX puts on every request it issues. Read instead of `X-Requested-With`,
# which jQuery and several other libraries also set — this rewrite must not fire for
# anything that cannot act on `HX-Redirect`.
HX_REQUEST = "HX-Request"
HX_REDIRECT = "HX-Redirect"

REDIRECT_STATUSES = frozenset(
    {
        HTTPStatus.MOVED_PERMANENTLY,
        HTTPStatus.FOUND,
        HTTPStatus.SEE_OTHER,
        HTTPStatus.TEMPORARY_REDIRECT,
        HTTPStatus.PERMANENT_REDIRECT,
    },
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
        """Count the request, refuse an overflowed bucket, rewrite on the way out."""
        if self._is_limited(request):
            return self._too_many()
        return self._as_hx_redirect(request, self.get_response(request))

    @classmethod
    def _as_hx_redirect(
        cls,
        request: HttpRequest,
        response: HttpResponseBase,
    ) -> HttpResponseBase:
        """Turn the login redirect into an `HX-Redirect` when HTMX is the caller.

        Scoped to the login destination rather than to every redirect on purpose: a
        Post/Redirect/Get answer to an HTMX form submission is a redirect HTMX is meant
        to follow and swap, and rewriting those would break every confirmation screen.

        The destination is the redirect's own `Location`, so whatever `next` the view
        put there survives and the accountant returns to the page they were reading.
        """
        if not cls._is_login_redirect(request, response):
            return response
        redirect = HttpResponse(status=HTTPStatus.NO_CONTENT)
        redirect[HX_REDIRECT] = response["Location"]
        return redirect

    @staticmethod
    def _is_login_redirect(request: HttpRequest, response: HttpResponseBase) -> bool:
        if request.headers.get(HX_REQUEST) is None:
            return False
        if response.status_code not in REDIRECT_STATUSES:
            return False
        location = response.headers.get("Location")
        if location is None:
            return False
        # `reverse` reads the resolver's cached URL map, never the database, which is
        # what keeps this safe inside the portal transaction.
        return urlsplit(location).path == reverse(settings.LOGIN_URL)

    @staticmethod
    def _is_limited(request: HttpRequest) -> bool:
        if is_public_post_endpoint(request):
            # Both applicable buckets are counted, not short-circuited: an attacker
            # spreading attempts must still exhaust the aggregate per-IP umbrella.
            overflowed = [
                exceeds(request, limit) for limit in credential_limits(request)
            ]
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
