"""The firm search endpoint behind the header field and the `Ctrl/Cmd+K` palette.

One GET, two renderings of the same `search()` answer: an HTMX request gets the
listbox fragment the palette swaps in, and anything else -- the header form
submitted with or without script -- gets a full results page of ordinary links.

Firm-side only. The portal has its own URL configuration and no route here, and a
firm without `cycles_r1` is answered 404 like every other R1 surface. Decorator
order is the registry's: the firm is resolved before the capability is consulted,
so the platform host gets a 404 rather than a 403 that would confirm the screen.
"""

from itertools import groupby

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from apps.accounts.views import AuthenticatedRequest
from apps.authz.services import require_can
from apps.clients.views import require_tenant
from apps.core.flags import require_feature
from apps.core.search import KIND_LABELS, MIN_QUERY_LENGTH, SearchHit, search

SEARCH_FLAG = "cycles_r1"
QUERY_PARAM = "q"


def _groups(hits: list[SearchHit]) -> list[tuple[str, str, list[SearchHit]]]:
    return [
        (kind.value, KIND_LABELS[kind], list(rows))
        for kind, rows in groupby(hits, key=lambda hit: hit.kind)
    ]


@require_http_methods(["GET"])
@login_required
@require_tenant
@require_feature(SEARCH_FLAG)
@require_can("clients.view_assigned")
def search_view(request: AuthenticatedRequest) -> HttpResponse:
    """Answer the header form with a page and the palette with a listbox fragment."""
    query = request.GET.get(QUERY_PARAM, "")
    hits = search(request.user, query)
    context = {
        "query": query,
        "groups": _groups(hits),
        "too_short": len(query.strip()) < MIN_QUERY_LENGTH,
        "min_length": MIN_QUERY_LENGTH,
    }
    if request.headers.get("HX-Request") == "true":
        return render(request, "core/search_results_listbox.html", context)
    return render(request, "core/search.html", context)
