"""The portal landing page -- the tracer bullet for the whole isolation stack.

This view is deliberately thin, because what it proves is not its own logic. It
runs as `app_portal` inside `PortalMiddleware`'s transaction, so every row it
renders has already passed the RESTRICTIVE policies comparing `app.client_id`.
If the isolation is wrong this page shows another firm's client or the wrong
obligation count; if it is right, the code below needs no filtering of its own.

**Every table touched here must be one of the six `app_portal` holds SELECT
on.** `request.client` and `request.user` are already materialised by the
middleware and cost no query. `clients_clientcompany` and
`obligations_obligation` are both allow-listed. Reaching for `{{ perms.* }}`
(`auth_permission`), `tenants_tenant` or `mfa_authenticator` from the template
raises `permission denied`, which is why the template renders `request.client`
rather than `request.tenant`.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from http import HTTPStatus
from typing import Any, Final

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.paginator import EmptyPage, Page, Paginator
from django.db.models import Count, Q, QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from apps.accounts.models import User
from apps.authz.services import require_can
from apps.clients.models import ClientCompany
from apps.obligations.models import Document, Obligation
from apps.obligations.queries import SETTLED
from apps.portal.middleware import PortalHttpRequest

# FULL for both client roles, which core.E010 requires of every portal gate.
VAULT_CAPABILITY = "documents.transfer"

# Twelve months of DAS on one screen, which is the span a MEI owner thinks in.
PAYMENTS_PAGE_SIZE: Final = 12
# Spelled as the firm side spells it, so one product has one word for a page number.
PAYMENTS_PAGE_PARAM: Final = "pagina"

# Twelve rows again, matching the payments window rather than choosing a size of its
# own: the two lists sit one tap apart in the same navigation bar, and a client moving
# between them should not have to learn a second rhythm. Kept as a separate constant
# because the REASONS differ -- twelve months of DAS is a calendar, twelve documents is
# a screenful -- and a shared one would tie the two together on nothing but the number
# happening to agree today.
DOCUMENTS_PAGE_SIZE: Final = 12

KIB: Final = 1024
MIB: Final = KIB * KIB

MISSING_FILE: Final = "missing"
OVERSIZED: Final = "oversized"

# Success AND what it means. "Enviado com sucesso" tells a MEI owner the byte transfer
# worked, which is not what she was asking; she sent the file so that somebody would act
# on it, and this is the sentence that answers the question she actually had.
UPLOAD_CONFIRMATION = _("Documento enviado. Sua contadora já pode vê-lo.")


class PortalVaultRequest(PortalHttpRequest):
    """A portal request past `login_required`, so `user` is never anonymous.

    Narrower than either parent alone: `PortalHttpRequest` supplies tenant and client,
    and this adds the guarantee `login_required` already enforces at runtime. Written as
    a type rather than asserted in each view, so the compiler carries it.
    """

    user: User
    client: ClientCompany


@dataclass(frozen=True, slots=True)
class NextAction:
    """The one obligation a client should deal with next, flattened to scalars.

    Deliberately not the `Obligation` row. A model instance handed to a portal template
    is a foreign key waiting to be followed, and following one during rendering means a
    join onto `obligations_obligationtype` -- a table `app_portal` holds no SELECT on --
    from inside the transaction and under the portal role. Passing values the view has
    already read removes the possibility instead of documenting it.

    `type_code` is the code itself and never a label. Naming these obligations is
    presentation and belongs in the template: `tests/obligations/test_due_rules.py`
    forbids the quoted code literal anywhere under `apps/`, so that the deadline engine
    stays driven by its seeded `due_rule` rows rather than by a ladder of branches on
    the code, and it cannot tell a display label apart from the branch it exists to
    prevent.
    """

    type_code: str
    competence_month: date
    nominal_due_date: date
    due_date: date
    days_until: int
    rolled: bool


@dataclass(frozen=True, slots=True)
class Payment:
    """One row of the payments list, flattened to scalars for the same reason.

    `NextAction` above explains it at length and every word applies here twelve times
    over: a page holding model instances is a page one `{{ row.obligation_type.name }}`
    away from joining onto a table the portal role cannot read.

    `status` is the raw column. The pt-BR word beside it and the sentence explaining
    what it costs are presentation, and the template owns both.
    """

    type_code: str
    competence_month: date
    nominal_due_date: date
    due_date: date
    rolled: bool
    status: str


def _situation(today: date) -> tuple[int, int]:
    """Return how many obligations this client has, and how many of them are late.

    One statement for both figures. They are read off the same rows and this page is
    opened on a phone, so two `.count()` calls would be two round trips for one answer.

    Late is decided by the DATE, never by the status column, exactly as the firm-side
    queues decide it. There is an `overdue` status and a nightly sweep that writes it,
    and a page trusting that label tells a client they are in good standing on any
    morning the scheduler did not run -- which is this product's worst failure mode
    wearing a reassuring face.
    """
    totals = Obligation.objects.aggregate(
        total=Count("pk"),
        overdue=Count(
            "pk",
            filter=Q(resolved_due_date__lt=today) & ~Q(status__in=SETTLED),
        ),
    )
    return int(totals["total"]), int(totals["overdue"])


def _next_action(today: date) -> NextAction | None:
    """Return the earliest unsettled obligation still ahead, or `None` if there is none.

    `.values()` rather than a model instance, and `obligation_type_id` rather than the
    relation: the code IS the primary key of `ObligationType`, so the local column
    already carries everything the page needs and the query trims the join away. `pk`
    breaks ties, so two obligations sharing a due date resolve to the same one on every
    request instead of alternating between renders.
    """
    row = (
        Obligation.objects.exclude(status__in=SETTLED)
        .filter(resolved_due_date__gte=today)
        .order_by("resolved_due_date", "pk")
        .values(
            "obligation_type_id",
            "competence_month",
            "nominal_due_date",
            "resolved_due_date",
        )
        .first()
    )
    if row is None:
        return None
    due = row["resolved_due_date"]
    nominal = row["nominal_due_date"]
    return NextAction(
        type_code=row["obligation_type_id"],
        competence_month=row["competence_month"],
        nominal_due_date=nominal,
        due_date=due,
        # Computed here because the template cannot subtract two dates and the portal
        # ships no JavaScript that could. A client reading "vence em 3 dias" is reading
        # a number this process produced.
        days_until=(due - today).days,
        rolled=nominal != due,
    )


@login_required
def portal_home(request: HttpRequest) -> HttpResponse:
    """Render the client's own company, where they stand, and what to do next."""
    client = getattr(request, "client", None)
    # No client filter is applied on purpose, here or in either helper below. The
    # RESTRICTIVE portal policy on obligations_obligation compares app.client_id, so
    # every figure and the row behind the card are already confined to the one client —
    # and writing `.filter(client=client)` would make the test that drops the policy
    # pass anyway, hiding the very failure it exists to catch.
    today = timezone.localdate()
    obligation_count, overdue_count = _situation(today)
    return render(
        request,
        "portal/home.html",
        {
            "client": client,
            "obligation_count": obligation_count,
            "overdue_count": overdue_count,
            "next_action": _next_action(today),
        },
    )


def _payments_page[Row](
    rows: QuerySet[Obligation, Row],
    request: HttpRequest,
) -> Page[Row]:
    """Return the requested slice of the client's book, first page for unusable input.

    Generic in the row rather than pinned to `dict[str, Any]`, because django-stubs
    types `.values("a", "b")` as a TypedDict of exactly those columns — an annotation
    naming the dict would reject the one call this helper exists to serve.

    An out-of-range or non-numeric page is a stale bookmark or a typed address, not an
    error worth a 404 in the middle of a working session.

    This is the ONLY querystring parameter the page reads, and it selects nothing: it
    moves a window over rows the database has already confined. A narrowing parameter
    would be a different thing entirely — a client who can filter a confined list can
    interrogate it, because the shape of the answer reports on the rows the policy is
    keeping out of reach.
    """
    paginator = Paginator(rows, PAYMENTS_PAGE_SIZE)
    raw = request.GET.get(PAYMENTS_PAGE_PARAM, "1")
    try:
        return paginator.page(int(raw))
    except (ValueError, EmptyPage):
        return paginator.page(1)


def _payment(row: Mapping[str, Any]) -> Payment:
    """Flatten one `.values()` row into the scalars the card draws."""
    nominal = row["nominal_due_date"]
    due = row["resolved_due_date"]
    return Payment(
        # The local column, never the relation. It already carries the code, because
        # the code IS `ObligationType`'s primary key.
        type_code=row["obligation_type_id"],
        competence_month=row["competence_month"],
        nominal_due_date=nominal,
        due_date=due,
        rolled=nominal != due,
        status=row["status"],
    )


@require_http_methods(["GET"])
@login_required
def portal_payments(request: HttpRequest) -> HttpResponse:
    """Render every obligation this client owes, newest competence month first.

    **Signed in is the whole gate, and that is a decision rather than an omission.**
    `core.E010` requires any capability a portal view names to be FULL for *both*
    client roles, and none of the payment-side capabilities is: `das.generate` is
    VIEW_CONFIRM for `client_owner`, so an object-less `require_can` would 403 the MEI
    owner on her own payment list. Row-level security is the control here — the
    RESTRICTIVE policy comparing `app.client_id` confines every row below — exactly as
    it is on `portal_home` above. Adding `require_can` to "tighten" this would not
    tighten anything; it would lock both client roles out of a page the database was
    already confining correctly.

    No client filter is applied, on purpose. `.filter(client=client)` would make the
    confinement test pass with the RESTRICTIVE policy dropped, hiding the very failure
    that test exists to catch.

    `.values()` rather than model instances, and the columns are all local to
    `obligations_obligation`. A model instance handed to a portal template is a foreign
    key waiting to be followed, and following `obligation_type` during rendering means a
    join onto `obligations_obligationtype` — a table `app_portal` holds no SELECT on —
    from inside the transaction and under the portal role. Passing values the view has
    already read removes the possibility instead of documenting it.

    `pk` breaks ties after the competence month, so a client with two obligations in one
    month reads them in the same order on every request rather than alternating between
    renders and between pages.
    """
    rows = Obligation.objects.order_by("-competence_month", "pk").values(
        "obligation_type_id",
        "competence_month",
        "nominal_due_date",
        "resolved_due_date",
        "status",
    )
    page = _payments_page(rows, request)
    return render(
        request,
        "portal/payments.html",
        {
            "page": page,
            "payments": [_payment(row) for row in page.object_list],
        },
    )


def _size_label(byte_size: int) -> str:
    """Render a byte count the way a person reads one, with a comma decimal mark.

    Computed here because the template can divide nothing and the portal ships no
    JavaScript that could. Deliberately NOT `filesizeformat`: that filter renders
    through Django's active locale, which a request influences with `Accept-Language`,
    and a client sizing a file against "10.0 MB" on one visit and "10,0 MB" on the next
    is reading two different promises about the same cap.
    """
    if byte_size >= MIB:
        return f"{byte_size / MIB:.1f}".replace(".", ",") + " MB"
    if byte_size >= KIB:
        return f"{byte_size / KIB:.1f}".replace(".", ",") + " kB"
    return f"{byte_size} bytes"


def _documents_page(
    rows: QuerySet[Document],
    request: HttpRequest,
) -> Page[Document]:
    """Return the requested slice of the client's vault, first page for bad input.

    `_payments_page` above is the same shape and is deliberately not shared: making one
    helper serve both would mean editing the payments call site, and this todo does not
    touch that view. The querystring word IS shared, because one product has one word
    for a page number and two constants both spelling "pagina" is one rename away from
    being two different words.

    As there, this is the only parameter the page reads and it selects nothing: it moves
    a window over rows the database has already confined. A NARROWING parameter would be
    a different thing entirely -- someone who can filter a confined list can interrogate
    it, because the shape of the answer reports on the rows the policy is keeping out of
    reach.
    """
    paginator = Paginator(rows, DOCUMENTS_PAGE_SIZE)
    raw = request.GET.get(PAYMENTS_PAGE_PARAM, "1")
    try:
        return paginator.page(int(raw))
    except (ValueError, EmptyPage):
        return paginator.page(1)


@require_http_methods(["GET"])
@login_required
@require_can(VAULT_CAPABILITY)
def portal_documents(request: PortalVaultRequest) -> HttpResponse:
    """Render this client's own evidence, newest first, above the form that adds to it.

    **The gate is `require_can`, and that is a real difference from `portal_payments`
    rather than an inconsistency.** `core.E010` requires any capability a portal view
    names to be FULL for BOTH client roles. No payment-side capability is —
    `das.generate` is VIEW_CONFIRM for `client_owner` — so an object-less `require_can`
    there would 403 a MEI owner on her own payment list, and sign-in is the whole gate.
    `documents.transfer` IS FULL for both, which is why the upload and download siblings
    below already carry it and why this page carries it too. The two screens genuinely
    differ; harmonising them would either lock both client roles out of payments or drop
    a gate the vault is entitled to.

    No client filter is applied, on purpose, exactly as on the two pages above. The
    RESTRICTIVE policy comparing `app.client_id` confines every row, and
    `.filter(client=client)` would make the confinement test pass with that policy
    dropped — hiding the very failure the test exists to catch.

    **Model instances rather than `.values()`, which inverts the payments reasoning
    deliberately.** There, flattening to scalars stops a template following
    `obligation_type` onto a table `app_portal` holds no SELECT on. Here it would buy
    the opposite of what it looks like it buys. This model's foreign keys point at
    `accounts_user`, `obligations_obligation` and `clients_clientcompany`; a `.values()`
    row renders `{{ document.uploaded_by.email }}` as an empty string, so a template
    reaching for the uploader would produce a page that looked entirely correct while
    the structural guard was the only thing that ever noticed. Handing the template the
    row makes that same mistake `permission denied` on the spot — loud, in development,
    on the first render — and the guard becomes a second line of defence rather than the
    only one.

    The size beside each row is paired with the row rather than attached to it: the page
    needs one figure this model does not store, and computing it here keeps the template
    rendering values instead of deriving them.
    """
    rows = Document.objects.order_by("-created_at", "pk")
    page = _documents_page(rows, request)
    return render(
        request,
        "portal/documents.html",
        {
            "page": page,
            "documents": [
                (document, _size_label(document.byte_size))
                for document in page.object_list
            ],
            "max_size_label": _size_label(settings.PORTAL_UPLOAD_MAX_BYTES),
        },
    )


@require_http_methods(["GET"])
@login_required
def portal_account(request: HttpRequest) -> HttpResponse:
    """Render the three things a client can do to their own sign-in, and nothing else.

    **Zero data reads, and no context, which is the finished shape rather than a page
    still waiting to be filled in.** Every destination this screen offers is a static
    address the URL tree already carries, and the one value it shows —
    `request.user.email` — is materialised by the middleware BEFORE the transaction
    opens and before the role switch, so the template reads it without issuing
    anything. `tests/portal/test_portal_templates_render.py:102-106` allows exactly
    three attributes through `request.user` for that reason, and this page is the one
    the allowance was written for. There is consequently no queryset here to confine,
    and the `.filter(client=…)` discussion that runs through the three views above
    simply does not arise: a page that reads nothing cannot leak a row.

    **Signed in is the whole gate, and it is a decision rather than an omission.** The
    reasoning is `portal_payments`' reasoning arriving at the same place by a shorter
    road. `core.E010` requires any capability a portal view NAMES to be FULL for both
    client roles, and it reaches that check through `PORTAL_CAPABILITY_GATES`, which
    only knows about gated callbacks — an ungated one is skipped
    (`apps/core/checks.py:375-377`). So `require_can` here would not be a free
    tightening. It would have to name a capability, every capability it could plausibly
    name is below FULL for at least one of the two client roles, and the result would be
    a MEI owner 403'd out of the page that holds her own password-change link. Nothing
    is being protected by that trade, because there is no row to protect: the page reads
    no table, and the three addresses it links are each guarded by the view behind them.

    The links are named, not built. `account_logout` is reached by a POST form in the
    template rather than an anchor, because a signing-out GET is a link any prefetch,
    scanner or preloading browser can follow on the client's behalf.
    """
    return render(request, "portal/account.html")


def _upload_refused(request: PortalVaultRequest, reason: str) -> HttpResponse:
    """Answer a refused upload with a page a person can read, still at 400.

    **The status is the part that must not move.** Two cases in
    `tests/portal/test_document_vault.py` pin `BAD_REQUEST` on these branches, and they
    pin it because the cap is a real control: an authenticated caller is the one party
    who fully controls how much this container writes to disk. What changes here is only
    what the client SEES. A bare `HttpResponseBadRequest` renders one line of unstyled
    text on a blank page, which is indistinguishable from the site being broken — so a
    MEI owner whose photo of a nota fiscal was a megabyte too large is told nothing
    about which of the two happened, or what to do next.

    The page carries the upload form again rather than a link back to it. A refusal that
    dead-ends is a client who gives up, and a file input cannot be pre-filled anyway, so
    the retry costs exactly one form and saves a navigation.
    """
    return render(
        request,
        "portal/upload_error.html",
        {
            "reason": reason,
            "max_size_label": _size_label(settings.PORTAL_UPLOAD_MAX_BYTES),
        },
        status=HTTPStatus.BAD_REQUEST,
    )


@require_http_methods(["POST"])
@login_required
@require_can(VAULT_CAPABILITY)
def document_upload(request: PortalVaultRequest) -> HttpResponse:
    """Store one uploaded file against the signed-in client.

    `documents.transfer` is FULL for both client roles, which is what `core.E010`
    requires of any portal gate: `require_can` passes no object, and below FULL that is
    an unconditional 403 rather than a refusal anyone chose.

    The row is attributed from `request.client`, never the request body. That is not
    a validation shortcut -- the RESTRICTIVE `WITH CHECK` on this table compares
    `app.client_id`, so a forged `client_id` is refused by the database, and reading it
    from the caller would only move where the refusal happens while adding a way to get
    it wrong.
    """
    upload = request.FILES.get("file")
    if upload is None:
        return _upload_refused(request, MISSING_FILE)

    # Checked before anything is read or stored. `upload.size` comes from the
    # Content-Length Django already parsed, so this refuses before the bytes are
    # buffered rather than after.
    # `size` is Optional on the base class, and a None here would silently skip the cap
    # rather than refuse -- so an unknown size is refused outright.
    if upload.size is None or upload.size > settings.PORTAL_UPLOAD_MAX_BYTES:
        return _upload_refused(request, OVERSIZED)

    payload = upload.read()
    obligation_id = request.POST.get("obligation") or None
    document = Document(
        tenant=request.tenant,
        client=request.client,
        obligation_id=obligation_id,
        sha256=hashlib.sha256(payload).hexdigest(),
        original_filename=upload.name or "documento",
        content_type=upload.content_type or "application/octet-stream",
        byte_size=len(payload),
        uploaded_by=request.user,
    )
    default_storage.save(document.storage_key, ContentFile(payload))
    document.save(force_insert=True)
    # Back to the vault rather than to the landing page, so the client lands on the list
    # their upload just joined and can see it there. The confirmation is queued rather
    # than rendered here: this response is a redirect, and a message written into the
    # session survives it because the session is written back in a response phase that
    # runs OUTSIDE the portal transaction, after the role and both GUCs are gone.
    messages.success(request, UPLOAD_CONFIRMATION)
    return redirect("portal-documents")


@require_http_methods(["GET"])
@login_required
@require_can(VAULT_CAPABILITY)
def document_download(
    request: PortalVaultRequest,  # noqa: ARG001
    storage_key: str,
) -> HttpResponse:
    """Return one document's bytes, authorised against the row before storage is asked.

    **W6.** The lookup is the refusal. `obligations_document` carries a RESTRICTIVE
    policy comparing `app.client_id`, so another client's key matches no row and this
    raises `Http404` -- before a single byte is read, and before the storage client is
    invoked at all. That ordering is the requirement, not an optimisation: with the
    storage call first, a replayed key would fetch the object and be refused afterwards,
    which is a different and much weaker property.

    Never a `StreamingHttpResponse`. `PortalMiddleware._reject_streaming` refuses one,
    because its iterator would be consumed after the transaction closes and the role and
    both GUCs are gone, yielding nothing.
    """
    document = get_object_or_404(Document, storage_key=storage_key)

    # Size from metadata BEFORE the body. Under object storage this is a HEAD rather
    # than
    # a GET, so an oversized document is refused without transferring it.
    if default_storage.size(document.storage_key) > settings.PORTAL_DOCUMENT_MAX_BYTES:
        return HttpResponseBadRequest("Documento excede o limite de transferência.")

    with default_storage.open(document.storage_key) as handle:
        payload = handle.read()

    response = HttpResponse(payload, content_type=document.content_type)
    response["Content-Disposition"] = (
        f'attachment; filename="{document.original_filename}"'
    )
    return response
