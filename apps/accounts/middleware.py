"""Force firm-side accounts and platform operators into MFA enrolment.

Registered **before** `TenantMiddleware`: the requirement is a property of the account,
not of the firm being visited, so it can be settled without resolving a tenant or
opening the tenant transaction. Deciding earlier also means an un-enrolled operator
never reaches a code path that reads tenant data at all.
"""

from collections.abc import Callable

from allauth.mfa.models import Authenticator
from allauth.mfa.utils import is_mfa_enabled
from django.conf import settings
from django.http import HttpRequest, HttpResponseBase, HttpResponseRedirect
from django.urls import reverse

from apps.accounts.mfa import MFA_ENROLMENT_URL_NAME, is_exempt_path, requires_mfa


class MFAEnforcementMiddleware:
    """Redirect gated accounts to TOTP enrolment until they carry an authenticator."""

    def __init__(
        self,
        get_response: Callable[[HttpRequest], HttpResponseBase],
    ) -> None:
        """Store the next callable in the middleware chain."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        """Serve the request, or divert it to enrolment first."""
        if self._must_enrol(request):
            return HttpResponseRedirect(reverse(MFA_ENROLMENT_URL_NAME))
        return self.get_response(request)

    @staticmethod
    def _must_enrol(request: HttpRequest) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        if is_exempt_path(request.path_info, _static_prefixes()):
            return False
        if not requires_mfa(user):
            return False
        # WebAuthn is deliberately absent from this list. Recovery codes are too: a
        # user holding only recovery codes has no working second factor, merely a way
        # to bypass one that does not exist.
        return not is_mfa_enabled(user, [Authenticator.Type.TOTP])


def _static_prefixes() -> tuple[str, ...]:
    return tuple(
        prefix
        for prefix in (settings.STATIC_URL, settings.MEDIA_URL)
        if prefix and prefix.startswith("/")
    )
