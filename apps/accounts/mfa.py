"""Who must carry a second factor, and which paths the requirement cannot cover.

allauth supplies the machinery — TOTP enrolment, verification, recovery codes — but
ships no "require MFA for this population" control, and the decision record in
`docs/decisions/0001-mfa-provider.md` says so explicitly. That policy is ours, and it
is written here rather than inside the middleware so it can be asserted directly.

The population is deliberately a **union**, not just firm membership:

* anyone holding an active `Membership` is firm-side and handles client tax data;
* anyone with `is_staff` reaches the platform admin console, which spans every firm.

A platform operator may hold `is_staff` and no `Membership` at all. Gating on
membership alone therefore reads as complete while leaving the single most privileged
surface in the product unprotected.
"""

from django.contrib.auth.models import AnonymousUser

from apps.accounts.models import User
from apps.tenants.models import Membership

MFA_ENROLMENT_URL_NAME = "mfa_activate_totp"

# Prefixes the gate must never guard, each for a different reason:
#
#   /accounts/  is allauth's own tree. It holds the enrolment page the gate redirects
#               to, so guarding it would be an infinite loop rather than a control. It
#               also holds logout, which a half-enrolled user must always be able to
#               reach.
#   /healthz    is the liveness probe. An orchestrator must not restart a healthy
#               process because the operator with a session open has not enrolled.
EXEMPT_PREFIXES: tuple[str, ...] = ("/accounts/", "/healthz")


def requires_mfa(user: User | AnonymousUser) -> bool:
    """Report whether this account is required to carry a second factor."""
    if not user.is_authenticated:
        return False
    if user.is_staff:
        return True
    # PLATFORM_QUERY_OK: filtered on the very account whose policy is being
    # evaluated, and returns a boolean rather than any row.
    return Membership.objects.filter(user=user, is_active=True).exists()


def is_exempt_path(path: str, extra_prefixes: tuple[str, ...] = ()) -> bool:
    """Report whether a path is outside the gate's remit."""
    return path.startswith((*EXEMPT_PREFIXES, *extra_prefixes))
