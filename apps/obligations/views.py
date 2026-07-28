"""The four queue screens.

Four view functions rather than one dispatching on a path segment, because each queue
is guarded by a **different** capability and `require_can` takes a static one. Reading
DAS deadlines is `das.generate` work, which the published matrix refuses an operations
admin outright; the revenue queue is `reports.view_financial`, which that role holds
only as `limited` — and `can()` answers False for a refined level with no object in
hand, which is the correct answer for a list. Collapsing the four into one gate would
mean picking the most permissive of them.

The refusal is a 403 raised by the decorator, never a filter that returns nothing. A
list view that answers 200 with zero rows is the perfect disguise for a permission
bug, because "no rows" and "no work to do" render identically.

Nothing here streams. `TenantMiddleware` raises `TypeError` on a streaming response,
because its iterator is consumed after the middleware returns — outside the tenant
transaction — so every lazy query inside it would come back empty and the page would
render, perfectly, with nothing in it.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http import HTTPStatus
from typing import Final, Protocol, overload
from urllib.parse import urlencode
from uuid import UUID

from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, Page, Paginator
from django.db.models import QuerySet
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from apps.accounts.models import User
from apps.accounts.views import AuthenticatedRequest
from apps.authz.models import GrantLevel
from apps.authz.services import Actor, granted_levels, require_can
from apps.obligations.models import Obligation, ObligationStatus
from apps.obligations.queries import (
    due_soon,
    onboarding_blocked,
    overdue,
    threshold_warning,
)
from apps.tenants.models import Membership

PAGE_SIZE: Final = 25
PAGE_PARAM: Final = "pagina"
ASSIGNEE_PARAM: Final = "responsavel"
STATUS_PARAM: Final = "situacao"

HTMX_HEADER: Final = "HX-Request"


class Paginable(Protocol):
    """What a queue must return for `Paginator` to slice it lazily.

    Declared rather than imported: django-stubs names this shape `_SupportsPagination`
    and marks it type-check-only, so importing it would tie this module to a private
    name in a third-party stub package. Both shapes a queue returns satisfy it — a
    `QuerySet`, which slices into a LIMIT, and the threshold queue's tuple, which is
    already in memory because its band is arithmetic rather than a column.
    """

    def __len__(self) -> int:
        """Report how many rows there are, for the page count."""

    def __iter__(self) -> Iterator[object]:
        """Iterate the rows of one page."""

    @overload
    def __getitem__(self, index: int, /) -> object: ...

    @overload
    def __getitem__(self, index: slice, /) -> "Paginable": ...


@dataclass(frozen=True, slots=True)
class QueueSpec[RowsT: Paginable]:
    """One queue: its identity, the capability it needs, and how to fill it.

    Generic over what `fetch` returns, because that return type is exactly what
    decides whether the status filter is available: narrowing by status means calling
    `.filter()`, which `Paginable` does not have and the threshold queue's tuple
    genuinely cannot answer. A queue that *is* narrowable declares itself an
    `ObligationQueueSpec`, whose `fetch` returns a queryset by construction.

    `accepts_status` is therefore a property of the class rather than a flag on the
    constructor. The pairing it used to express — a tuple-returning `fetch` alongside
    `accepts_status=True` — was an invariant spanning two fields with nothing checking
    it, and getting it wrong meant `AttributeError: 'tuple' object has no attribute
    'filter'` at request time. It is now not expressible.
    """

    slug: str
    label: object
    capability: str
    fetch: Callable[..., RowsT]
    row_template: str
    headings: tuple[object, ...]

    @property
    def accepts_status(self) -> bool:
        """Report whether this queue can be narrowed by obligation status."""
        return False


@dataclass(frozen=True, slots=True)
class ObligationQueueSpec(QueueSpec[QuerySet[Obligation]]):
    """A queue of obligation rows, which the database can narrow by `status`.

    Adds no fields: the whole content of the subclass is the promise its type
    parameter makes about `fetch`, which is what lets the view call `.filter()`
    without an escape hatch.
    """

    @property
    def accepts_status(self) -> bool:
        """Report that these rows carry a status column the database can filter."""
        return True


OBLIGATION_HEADINGS: Final[tuple[object, ...]] = (
    _("Cliente"),
    _("Obrigação"),
    _("Competência"),
    _("Vencimento"),
    _("Situação"),
)

DUE_SOON = ObligationQueueSpec(
    "due-soon",
    _("Vencendo em 7 dias"),
    "das.generate",
    due_soon,
    "obligations/_rows_obligations.html",
    OBLIGATION_HEADINGS,
)
OVERDUE = ObligationQueueSpec(
    "overdue",
    _("Atrasadas"),
    "das.generate",
    overdue,
    "obligations/_rows_obligations.html",
    OBLIGATION_HEADINGS,
)
ONBOARDING = QueueSpec(
    "onboarding-blocked",
    _("Onboarding travado"),
    "clients.view_assigned",
    onboarding_blocked,
    "obligations/_rows_onboarding.html",
    (_("Cliente"), _("Item"), _("Motivo"), _("Situação")),
)
THRESHOLD = QueueSpec(
    "threshold-warning",
    _("Limite de faturamento"),
    "reports.view_financial",
    threshold_warning,
    "obligations/_rows_threshold.html",
    (_("Cliente"), _("Faturado"), _("Teto"), _("Consumido"), _("Faixa")),
)

ALL_QUEUES: Final[tuple[QueueSpec[Paginable], ...]] = (
    DUE_SOON,
    OVERDUE,
    ONBOARDING,
    THRESHOLD,
)

URL_NAMES: Final[dict[str, str]] = {
    DUE_SOON.slug: "queue-due-soon",
    OVERDUE.slug: "queue-overdue",
    ONBOARDING.slug: "queue-onboarding",
    THRESHOLD.slug: "queue-threshold",
}


def _tenant_id(request: HttpRequest) -> UUID:
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404
    return UUID(str(tenant.pk))


def _members(request: AuthenticatedRequest) -> list[User]:
    """Return the accounts a queue may be filtered by: this firm's roster, only."""
    return [
        membership.user
        for membership in Membership.objects.for_user(request.user)
        .filter(tenant_id=_tenant_id(request), is_active=True)
        .select_related("user")
        .order_by("user__email")
    ]


def _requested_assignee(
    request: AuthenticatedRequest,
    members: list[User],
) -> User | None:
    """Resolve the assignee filter against this firm's roster, ignoring anything else.

    Matched against the roster rather than looked up by id. Honouring an arbitrary
    user id would turn an empty queue into an existence oracle for other firms'
    accounts — ask for an id, and a queue that empties tells you it was real.
    """
    raw = request.GET.get(ASSIGNEE_PARAM, "").strip()
    if not raw:
        return None
    return next((member for member in members if str(member.pk) == raw), None)


def _requested_status(request: HttpRequest) -> str | None:
    """Resolve the status filter, ignoring a value the enum does not define."""
    raw = request.GET.get(STATUS_PARAM, "").strip()
    return raw if raw in ObligationStatus.values else None


def _page(rows: Paginable, request: HttpRequest) -> Page[object]:
    """Return the requested page, falling back to the first for unusable input.

    An out-of-range or non-numeric page is a stale bookmark or a typed URL, not an
    error worth a 404 in the middle of a working session.
    """
    paginator = Paginator(rows, PAGE_SIZE)
    raw = request.GET.get(PAGE_PARAM, "1")
    try:
        return paginator.page(int(raw))
    except (ValueError, EmptyPage):
        return paginator.page(1)


def _filter_query(assignee: User | None, status: str | None) -> str:
    """Render the active filters as a querystring prefix for the pagination links.

    Without it, paging away from a filtered view silently drops the filter and the
    accountant is looking at a different list than the one they were reading.
    """
    active = {}
    if assignee is not None:
        active[ASSIGNEE_PARAM] = str(assignee.pk)
    if status is not None:
        active[STATUS_PARAM] = status
    encoded = urlencode(active)
    return f"{encoded}&" if encoded else ""


def _render_queue(
    request: AuthenticatedRequest,
    spec: QueueSpec[Paginable],
) -> HttpResponse:
    members = _members(request)
    assignee = _requested_assignee(request, members)
    tenant_id = _tenant_id(request)

    # The isinstance is what makes `.filter()` legal rather than hopeful: on the base
    # spec `fetch` returns `Paginable`, which has no `.filter`, and the threshold queue
    # really does return a tuple. Only `ObligationQueueSpec` promises a queryset, and
    # only it reads the status parameter at all.
    rows: Paginable
    status: str | None
    if isinstance(spec, ObligationQueueSpec):
        status = _requested_status(request)
        obligations = spec.fetch(request.user, tenant_id=tenant_id, assignee=assignee)
        rows = obligations if status is None else obligations.filter(status=status)
    else:
        status = None
        rows = spec.fetch(request.user, tenant_id=tenant_id, assignee=assignee)

    context = {
        "spec": spec,
        "page": _page(rows, request),
        "members": members,
        "selected_assignee": str(assignee.pk) if assignee else "",
        "selected_status": status or "",
        "statuses": ObligationStatus.choices,
        "queue_url_name": URL_NAMES[spec.slug],
        "filter_query": _filter_query(assignee, status),
        "page_title": spec.label,
    }
    template = (
        "obligations/_queue.html"
        if request.headers.get(HTMX_HEADER)
        else "obligations/queue.html"
    )
    return render(request, template, context, status=HTTPStatus.OK)


@require_http_methods(["GET"])
@login_required
@require_can(DUE_SOON.capability)
def queue_due_soon(request: AuthenticatedRequest) -> HttpResponse:
    """Obligations resolving in the next seven days."""
    return _render_queue(request, DUE_SOON)


@require_http_methods(["GET"])
@login_required
@require_can(OVERDUE.capability)
def queue_overdue(request: AuthenticatedRequest) -> HttpResponse:
    """Obligations past their resolved due date and still unsettled."""
    return _render_queue(request, OVERDUE)


@require_http_methods(["GET"])
@login_required
@require_can(ONBOARDING.capability)
def queue_onboarding(request: AuthenticatedRequest) -> HttpResponse:
    """Checklist items a client's onboarding is stuck on."""
    return _render_queue(request, ONBOARDING)


@require_http_methods(["GET"])
@login_required
@require_can(THRESHOLD.capability)
def queue_threshold(request: AuthenticatedRequest) -> HttpResponse:
    """Clients at or past 80% of the ceiling that applies to them."""
    return _render_queue(request, THRESHOLD)


def queue_counts(request: AuthenticatedRequest) -> list[dict[str, object]]:
    """Count each queue this account may actually open, and no others.

    Omitting the rest is not tidiness: reporting "14 atrasadas" to somebody the
    overdue queue refuses hands them the number the refusal exists to withhold.
    """
    levels = granted_levels(
        request.user,
        [spec.capability for spec in ALL_QUEUES],
        tenant_id=_tenant_id(request),
    )
    return [
        {
            "spec": spec,
            "url_name": URL_NAMES[spec.slug],
            "count": len(
                spec.fetch(request.user, tenant_id=_tenant_id(request)),
            ),
        }
        for spec in ALL_QUEUES
        if levels[spec.capability] == GrantLevel.FULL
    ]


@require_http_methods(["GET"])
@login_required
@require_can("clients.view_assigned")
def queue_counts_view(request: AuthenticatedRequest) -> HttpResponse:
    """Serve the counts strip on its own, so HTMX can refresh it in place."""
    return render(
        request,
        "obligations/_counts.html",
        {"counts": queue_counts(request)},
    )


def visible_queue_links(user: Actor, tenant_id: UUID) -> list[dict[str, object]]:
    """Return the queue destinations this account may reach, for the dashboard."""
    levels = granted_levels(
        user,
        [spec.capability for spec in ALL_QUEUES],
        tenant_id=tenant_id,
    )
    return [
        {"spec": spec, "url_name": URL_NAMES[spec.slug]}
        for spec in ALL_QUEUES
        if levels[spec.capability] == GrantLevel.FULL
    ]


__all__ = [
    "ALL_QUEUES",
    "PAGE_SIZE",
    "ObligationQueueSpec",
    "QueueSpec",
    "queue_counts",
    "queue_counts_view",
    "queue_due_soon",
    "queue_onboarding",
    "queue_overdue",
    "queue_threshold",
    "visible_queue_links",
]
