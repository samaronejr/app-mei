"""Client registry views: the list screen, and the CSV export.

The two are gated differently on purpose. The export is `clients.view_all`, because a
file containing the whole registry is exactly what the `limited` level exists to
withhold. The list is `clients.view_assigned`, because a staff accountant's own book of
business is theirs to read — and what keeps that from becoming the whole firm is not
the capability but `visible_clients()`, which narrows an accountant to their
assignments *even though* the published matrix grants them `clients.view_all`.

Every row on the list therefore comes from `visible_clients()` and from nothing else.
`ClientCompany.objects.all()` would be scoped to the tenant and still wrong: it would
hand a staff accountant the firm's entire client base, silently, on the one screen
built for browsing it.
"""

from collections.abc import Callable
from functools import wraps
from http import HTTPStatus
from typing import Concatenate
from urllib.parse import urlencode
from uuid import UUID

from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, Page, Paginator
from django.db.models import Model, QuerySet
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from apps.accounts.forms import PortalInviteIssueForm
from apps.accounts.models import User
from apps.accounts.views import AuthenticatedRequest
from apps.audit.models import AuditAction
from apps.audit.services import record_event
from apps.authz.portfolio import visible_clients
from apps.authz.services import require_can
from apps.clients.exports import UTF8_BOM, build_client_csv
from apps.clients.managers import search_filter
from apps.clients.models import ClientCompany, OnboardingItem
from apps.core.dashboard import das_calendar
from apps.core.tenancy import current_tenant_id
from apps.fiscal.capabilities import capability_for
from apps.obligations.models import Obligation
from apps.obligations.queries import ThresholdRow, threshold_warning
from apps.tenants.models import Invite, Tenant

EXPORT_FILENAME = "clientes.csv"

# A directory rather than a worklist: the queues page at 25 because a short page is a
# unit of work to grind through, while this screen is scanned and searched, so a denser
# page means fewer round trips to reach a client whose name is already known.
PAGE_SIZE = 50
PAGE_PARAM = "pagina"
SEARCH_PARAM = "q"

# A year of history on the detail screen's first page, so "did we file last year's
# December?" is answered without paging. The registry's 50 would be three years of a
# monthly obligation and would bury the calendar strip below it.
OBLIGATION_PAGE_SIZE = 12


def _tenant(request: AuthenticatedRequest) -> Tenant:
    """Return the firm this request resolved to, or 404.

    The platform host resolves no tenant, and a registry with no firm behind it is not
    found rather than empty or refused. Both alternatives answer a question the caller
    should not be able to ask: an empty 200 asserts the firm exists and keeps no books,
    and a 403 asserts the screen is there and merely closed to this account.
    """
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404
    resolved: Tenant = tenant
    return resolved


def require_tenant[**P](
    view: Callable[Concatenate[AuthenticatedRequest, P], HttpResponse],
) -> Callable[Concatenate[AuthenticatedRequest, P], HttpResponse]:
    """Resolve the firm before the capability is consulted, or 404.

    **The position of this decorator is load-bearing and reordering it reintroduces an
    existence oracle.** `require_can` denies with a 403, and on the platform host it
    denies every account, because no firm-side membership resolves there. So a screen
    that consults the capability first answers "this screen exists and is closed to
    you" about a firm that is not there at all — which is a fact about the platform
    that anyone who can reach the host could then harvest. A 404 answers nothing.

    Stacked ABOVE `require_can`, this turns that request into the honest 404 and leaves
    the capability to decide only the cases where a firm really was found.

    Raising `Http404` from inside the view body — the shape `apps/core/dashboard.py`
    uses — cannot achieve this: the decorator has already returned 403 by then, which
    is exactly why `test_the_platform_host_has_no_dashboard` has to accept either
    status. That dashboard bug is real and is tracked separately; it is not fixed here.

    The signature is generic over the trailing parameters so the same decorator guards
    a view taking a URL argument. The detail screen receives a primary key, and a
    decorator that only accepted a bare request would force that screen to resolve the
    firm in its own body — precisely the shape documented above as unable to 404.
    """

    @wraps(view)
    def wrapped(
        request: AuthenticatedRequest,
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> HttpResponse:
        _tenant(request)
        return view(request, *args, **kwargs)

    return wrapped


def _searched(
    clients: QuerySet[ClientCompany],
    term: str,
) -> QuerySet[ClientCompany]:
    """Narrow the portfolio by the search box, or hand it back untouched.

    `search_filter` answers `None` for a blank term, and that case must skip the filter
    rather than apply it. `ClientCompanyManager.search("")` deliberately returns the
    empty queryset — right for an export, wrong here — so composing it unconditionally
    would blank the whole registry the moment an accountant cleared the box.

    A term that matches nothing is still applied. Falling back to the unfiltered
    registry for an unusable query would turn `?q=%` into a one-click export of every
    client the account can see, which is the failure the manager guards at its own
    layer.
    """
    matches = search_filter(term)
    if matches is None:
        return clients
    return clients.filter(matches)


def _page[ModelT: Model](
    clients: QuerySet[ModelT],
    request: HttpRequest,
    size: int = PAGE_SIZE,
) -> Page[ModelT]:
    """Return the requested page, falling back to the first for unusable input.

    An out-of-range or non-numeric page is a stale bookmark or a typed URL, not an
    error worth interrupting a working session over.
    """
    paginator = Paginator(clients, size)
    raw = request.GET.get(PAGE_PARAM, "1")
    try:
        return paginator.page(int(raw))
    except (ValueError, EmptyPage):
        return paginator.page(1)


def _filter_query(term: str) -> str:
    """Render the active search as a querystring prefix for the pagination links.

    Without it, paging away from a search silently drops it and the reader is looking
    at a different list than the one they were reading.
    """
    encoded = urlencode({SEARCH_PARAM: term}) if term else ""
    return f"{encoded}&" if encoded else ""


@require_http_methods(["GET"])
@login_required
@require_tenant
@require_can("clients.view_assigned")
def clients_list_view(request: AuthenticatedRequest) -> HttpResponse:
    """List the clients this account keeps books for, searchable and paginated.

    The ordering is `("legal_name", "pk")` and the trailing `pk` is load-bearing. Two
    clients may legitimately share a legal name, and under `("legal_name",)` alone the
    comparison between them is fully tied — PostgreSQL may then return them in a
    different order for each `LIMIT/OFFSET`, so a row slips between page one and page
    two and is never seen. An order is total exactly when it ends in a unique column.
    """
    tenant = _tenant(request)
    term = request.GET.get(SEARCH_PARAM, "").strip()

    clients = visible_clients(request.user, tenant_id=UUID(str(tenant.pk)))
    # `distinct` because the assignment lens joins: an accountant holding two
    # assignments on one client would otherwise read that client twice, on a screen
    # whose entire promise is that each row is one client.
    rows = _searched(clients, term).order_by("legal_name", "pk").distinct()

    return render(
        request,
        "clients/list.html",
        {
            "page": _page(rows, request),
            "term": term,
            "filter_query": _filter_query(term),
        },
        status=HTTPStatus.OK,
    )


def _band_for(
    request: AuthenticatedRequest,
    tenant_id: UUID,
    client: ClientCompany,
) -> ThresholdRow | None:
    """Return this client's revenue band, when the band is one worth showing.

    Read out of `threshold_warning` rather than computed here, so the detail screen and
    the dashboard's "perto do limite" table can never disagree about which band a
    client is in — and so the four bands keep exactly one definition. The queue costs a
    fixed number of queries however large the portfolio is
    (`apps/obligations/queries.py:15-20`), which is what makes scanning it for one
    client acceptable on a page budgeted per request rather than per client.

    `None` means the client is not in an attention band, and the screen then says
    nothing rather than inventing a reassurance. Absence here is the same absence the
    dashboard table already renders by simply not listing the client.
    """
    for row in threshold_warning(request.user, tenant_id=tenant_id):
        if row.client.pk == client.pk:
            return row
    return None


def client_detail_context(
    request: AuthenticatedRequest,
    client: ClientCompany,
    *,
    portal_invite_form: PortalInviteIssueForm | None = None,
) -> dict[str, object]:
    """Build detail context before a delivery failure marks rollback."""
    tenant = _tenant(request)
    tenant_id = UUID(str(tenant.pk))
    obligations = (
        Obligation.objects.filter(client=client)
        .select_related("obligation_type")
        .order_by("-competence_month", "pk")
    )
    if portal_invite_form is None:
        portal_invite_form = PortalInviteIssueForm()
    return {
        "client": client,
        "municipio": capability_for(client.municipality_ibge_code),
        "calendar": das_calendar(client),
        "calendar_client": client,
        "page": _page(obligations, request, OBLIGATION_PAGE_SIZE),
        "filter_query": "",
        "checklist": OnboardingItem.objects.filter(client=client).order_by(
            "position",
            "key",
            "pk",
        ),
        "banda": _band_for(request, tenant_id, client),
        "portal_invite_form": portal_invite_form,
        "pending_portal_invites": Invite.objects.for_user(request.user)
        .filter(
            tenant_id=tenant_id,
            client_id=client.pk,
            accepted_at__isnull=True,
            revoked_at__isnull=True,
        )
        .order_by("email"),
        "page_title": client.legal_name,
    }


@require_http_methods(["GET"])
@login_required
@require_tenant
@require_can("clients.view_assigned")
def clients_detail_view(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Show one client: who they are, what falls due, and what onboarding still owes.

    **The client is selected out of `visible_clients()` and a miss raises `Http404`.**
    Not `get_object_or_404(ClientCompany, pk=pk)`, and the difference is the whole
    security contract of this screen rather than a matter of taste. That manager is
    tenant-scoped, so the unscoped form would also refuse another firm's client — but
    it would refuse it as a *lookup*, leaving nothing to stop a staff accountant
    reading a client of their own firm they hold no assignment on. `visible_clients`
    is the only thing that narrows an accountant to their assignments, because the
    published matrix grants them `clients.view_all` outright
    (`apps/authz/portfolio.py:55-88`).

    And the refusal is 404, never 403. The URL carries the primary key, so a 403 would
    answer "this id names a real client of your firm and you are not on it" — turning
    the address bar into an enumeration oracle for the firm's whole book of business,
    one UUID at a time. `require_tenant` sits above `require_can` for the same reason
    one level up: on the platform host no firm-side membership resolves, so a
    capability consulted first would answer 403 about a firm that is not there.

    Read-only ABOUT THE RECORD, which is a narrower promise than the one this docstring
    used to make and is the one that actually protects the registry. There is still no
    edit, no upload and no delete: every field of a `ClientCompany` is fed by onboarding
    and by the CSV import, and a second write path here would be a second place a CNPJ
    can be entered unnormalized.

    The screen now carries exactly ONE control that writes, and it writes somewhere
    else. §17-G authorises the portal invitation from this page, because this is where
    an accountant already is when they decide a client should be able to see their own
    obligations — and the row it creates is a `tenants_invite`, never a column of the
    client being viewed. It posts to `portal-invite-issue`, whose own
    `@require_can(users.create)` is the authorization; the form below is handed to the
    template unbound so that the control can be drawn at all.

    `PortalInviteIssueForm()` is instantiated rather than resolved from anything, so it
    costs no query and cannot move the budget. What DOES cost is the template's own
    `{% can %}` on the same capability — three queries, bought for the reason the
    registry's export button bought its three: a control offered to an account that
    would be answered 403 teaches an accountant the product is broken.
    """
    tenant = _tenant(request)
    tenant_id = UUID(str(tenant.pk))

    client = visible_clients(request.user, tenant_id=tenant_id).filter(pk=pk).first()
    if client is None:
        raise Http404

    return render(
        request,
        "clients/detail.html",
        client_detail_context(request, client),
        status=HTTPStatus.OK,
    )


@require_http_methods(["GET"])
@require_can("clients.view_all")
def export_clients_csv(request: HttpRequest) -> HttpResponse:
    """Return the firm's client registry as a CSV attachment.

    **Materialized with `list()` before a single byte is written, and returned as a
    plain `HttpResponse`.** Returning a `StreamingHttpResponse` here would raise
    `TypeError` in `TenantMiddleware`, and that guard exists because the alternative
    is worse than an error: the iterator would run after the tenant transaction had
    closed, every query inside it would return zero rows under the fail-closed policy,
    and the accountant would receive an empty file that downloaded successfully.
    """
    companies = list(ClientCompany.objects.all().order_by("legal_name"))
    payload = build_client_csv(companies)

    tenant_id = current_tenant_id.get()
    if tenant_id is not None:
        # Bulk export of personal data is precisely the action LGPD art. 37 expects to
        # find in the record of processing operations, so it is audited even though
        # nothing was modified.
        record_event(
            action=AuditAction.EXPORT,
            tenant_id=tenant_id,
            actor=request.user if isinstance(request.user, User) else None,
            metadata={"kind": "clients_csv", "rows": len(companies)},
        )

    # The byte-order mark is what stops Excel reading a UTF-8 file as the system
    # codepage and turning every accented legal name into mojibake.
    response = HttpResponse(
        (UTF8_BOM + payload).encode("utf-8"),
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = f'attachment; filename="{EXPORT_FILENAME}"'
    return response
