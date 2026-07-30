"""A portal URL tree with probes, so the role and GUCs can be read mid-request.

Mirrors `apps/portal/urls.py` — same allauth mount at the same path, so anything
asserted here about rate limiting or exempt prefixes stays true of the real tree — and
adds views that report what the database actually sees while the request is open.
"""

from django.db import connection, transaction
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.urls import include, path


def probe(_request: HttpRequest) -> JsonResponse:
    """Report the effective role and both GUCs from inside the portal transaction."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_user, "
            "coalesce(current_setting('app.tenant_id', true), 'UNSET'), "
            "coalesce(current_setting('app.client_id', true), 'UNSET')",
        )
        row = cursor.fetchone()
    return JsonResponse(
        {"role": str(row[0]), "tenant_guc": str(row[1]), "client_guc": str(row[2])},
    )


def streamer(_request: HttpRequest) -> StreamingHttpResponse:
    """Return a streaming response, which a portal view must never do."""
    return StreamingHttpResponse(iter([b"x"]))


@transaction.non_atomic_requests
def no_database(_request: HttpRequest) -> JsonResponse:
    """Report whether a transaction was opened, and as which role.

    Returning a bare "ok" made the criterion untestable: a middleware that opened a
    transaction and switched the role anyway would revert both at commit, and the test
    could not tell the difference.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        row = cursor.fetchone()
    return JsonResponse(
        {"role": str(row[0]), "in_atomic": connection.in_atomic_block},
    )


def fake_admin(_request: HttpRequest) -> HttpResponse:
    """Stand in for the Django admin at /admin/.

    Mounted ON PURPOSE. Without a route here the resolver 404s /admin/ by itself, so
    the middleware's admin gate could be deleted entirely and its test would still
    pass -- the control would be decoration. With this route, only the gate produces
    the 404.
    """
    return HttpResponse("ADMIN REACHED")


urlpatterns = [
    path("probe", probe, name="portal-probe"),
    path("streamer", streamer, name="portal-streamer"),
    path("nodb", no_database, name="portal-nodb"),
    path("admin/", fake_admin, name="portal-fake-admin"),
    path("accounts/", include("allauth.urls")),
]
