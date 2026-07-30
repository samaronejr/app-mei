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
def no_database(_request: HttpRequest) -> HttpResponse:
    """A view that opted out of the request transaction."""
    return HttpResponse("ok")


urlpatterns = [
    path("probe", probe, name="portal-probe"),
    path("streamer", streamer, name="portal-streamer"),
    path("nodb", no_database, name="portal-nodb"),
    path("accounts/", include("allauth.urls")),
]
