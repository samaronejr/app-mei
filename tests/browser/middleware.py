"""Assert, on every live request, that the database session is a policed role.

Installed by `tests/browser/conftest.py` for browser tests only, as the innermost
middleware, so it runs inside the tenant transaction and after the portal's
`SET LOCAL ROLE app_portal`. It asks PostgreSQL which role the request is really
running as and records the answer.

Without the conftest's `connection_created` receiver, a live-server thread opens
its connection as `app_test` -- the table owner, BYPASSRLS -- and every page would
render every tenant's rows while looking perfectly normal. This is the check that
makes that failure loud: a wrong role is recorded, then refused with a 500, and the
browser test fails on the record with the role named.
"""

from collections.abc import Callable
from typing import Final

from django.db import connection
from django.http import HttpRequest
from django.http.response import HttpResponseBase

# Firm requests run as app_runtime; portal requests switch to app_portal inside
# their transaction. Anything else -- above all app_test -- bypasses the policies.
POLICED_ROLES: Final = frozenset({"app_runtime", "app_portal"})

# (path, role) for every live request the middleware saw. Appended from server
# threads and read by the test thread; list.append is atomic under the GIL.
OBSERVED: list[tuple[str, str]] = []


class EffectiveRoleMismatch(AssertionError):  # noqa: N818
    """A live request ran as a role the row-level-security policies do not bind."""


class EffectiveRoleMiddleware:
    """Record the effective role of every live request and refuse a wrong one."""

    def __init__(
        self,
        get_response: Callable[[HttpRequest], HttpResponseBase],
    ) -> None:
        """Store the next callable in the chain."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        """Ask the session who it is before the view runs."""
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            row = cursor.fetchone()
        role = str(row[0]) if row else ""
        OBSERVED.append((request.path, role))
        if role not in POLICED_ROLES:
            msg = (
                f"live request {request.path} ran as {role!r}, not one of "
                f"{sorted(POLICED_ROLES)}; row-level security was not in force"
            )
            raise EffectiveRoleMismatch(msg)
        return self.get_response(request)


MIDDLEWARE_PATH: Final = f"{__name__}.{EffectiveRoleMiddleware.__name__}"

__all__ = [
    "MIDDLEWARE_PATH",
    "OBSERVED",
    "POLICED_ROLES",
    "EffectiveRoleMiddleware",
    "EffectiveRoleMismatch",
]
