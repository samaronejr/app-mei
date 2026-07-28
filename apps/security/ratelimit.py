"""What gets limited, and — more importantly — what each limit is keyed on.

**The credential limits are keyed on email and IP, never on tenant, and that is the
whole point.** Credentials in this product are global: `accounts.User` is a
platform-wide table with `email` as its `USERNAME_FIELD`, so the same password works
on every firm's subdomain. A limit keyed on the tenant would let an attacker rotate
the `Host` header to mint a fresh bucket per firm — five attempts per minute *times
the number of firms on the platform* — and the correct-password signal is readable
from any subdomain, because during a login POST the user is not yet authenticated and
the membership check cannot yet apply. Tenant-keying a global credential is not a
weaker limit; it is a bypass with a limit-shaped API.

Tenant-keying is correct for the *authenticated* limits below it, where the caller has
been identified and a tenant has been resolved.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from django.conf import settings
from django.http import HttpRequest
from django.urls import Resolver404, resolve
from django_ratelimit.core import is_ratelimited

from apps.core.netaddr import client_ip

WRITE_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Limit:
    """One bucket: what it is called, how fast it refills, and what it counts."""

    group: str
    rate: str
    key: Callable[[str, HttpRequest], str]


def _submitted_email(request: HttpRequest) -> str:
    """Return the address a credential form is being submitted for.

    allauth names the field `login` when signing in and `email` when resetting a
    password. Normalized so that casing and whitespace cannot mint fresh buckets.
    """
    raw = request.POST.get("login") or request.POST.get("email") or ""
    return raw.strip().lower() or UNKNOWN


def _email_key(_group: str, request: HttpRequest) -> str:
    return f"email:{_submitted_email(request)}"


def _ip_key(_group: str, request: HttpRequest) -> str:
    return f"ip:{client_ip(request) or UNKNOWN}"


def _tenant_user_key(_group: str, request: HttpRequest) -> str:
    tenant = getattr(request, "tenant", None)
    user = getattr(request, "user", None)
    tenant_id = getattr(tenant, "id", None)
    user_id = getattr(user, "pk", None)
    return f"tenant:{tenant_id}:user:{user_id}"


def credential_limits() -> tuple[Limit, ...]:
    """Limits for endpoints that accept a password, keyed globally."""
    return (
        Limit("login-email", settings.RATELIMIT_LOGIN_EMAIL, _email_key),
        Limit("login-ip", settings.RATELIMIT_LOGIN_IP, _ip_key),
    )


def authenticated_limit(request: HttpRequest) -> Limit:
    """Return the limit for an identified caller: reads and writes differ."""
    if (request.method or "") in WRITE_METHODS:
        return Limit("write", settings.RATELIMIT_WRITE, _tenant_user_key)
    return Limit("read", settings.RATELIMIT_READ, _tenant_user_key)


def is_public_post_endpoint(request: HttpRequest) -> bool:
    """Report whether this is an unauthenticated POST that must not be floodable.

    Matched by URL *name* rather than by path prefix: allauth owns these routes and
    may move them, and a stale prefix would silently stop limiting the login form
    without failing anything.
    """
    if (request.method or "") != "POST":
        return False
    try:
        match = resolve(request.path_info, urlconf=getattr(request, "urlconf", None))
    except Resolver404:
        return False
    return match.url_name in set(settings.RATELIMIT_PUBLIC_POST_URL_NAMES)


def exceeds(request: HttpRequest, limit: Limit) -> bool:
    """Count this request against a bucket and report whether it just overflowed."""
    return bool(
        is_ratelimited(
            request=request,
            group=limit.group,
            key=limit.key,
            rate=limit.rate,
            method=is_ratelimited.ALL,
            increment=True,
        ),
    )
