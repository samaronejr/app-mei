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
from dataclasses import dataclass
from datetime import date

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.accounts.models import User
from apps.authz.services import require_can
from apps.clients.models import ClientCompany
from apps.obligations.models import Document, Obligation
from apps.obligations.queries import SETTLED
from apps.portal.middleware import PortalHttpRequest

# FULL for both client roles, which core.E010 requires of every portal gate.
VAULT_CAPABILITY = "documents.transfer"


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


@require_http_methods(["GET"])
@login_required
def portal_payments(request: HttpRequest) -> HttpResponse:
    """Render the client's deadlines. Placeholder until the list lands.

    The route exists now rather than with the page, because the shell's navigation
    names it on every portal screen and `{% url %}` on an unregistered name raises
    during rendering — so a bar pointing at a page that does not yet exist is not a
    dead link, it is a 500 on the whole portal.
    """
    return render(request, "portal/payments.html")


@require_http_methods(["GET"])
@login_required
def portal_documents(request: HttpRequest) -> HttpResponse:
    """Render the client's document vault. Placeholder until the list lands."""
    return render(request, "portal/documents.html")


@require_http_methods(["GET"])
@login_required
def portal_account(request: HttpRequest) -> HttpResponse:
    """Render the client's account page. Placeholder until the links land."""
    return render(request, "portal/account.html")


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
        return HttpResponseBadRequest("Nenhum arquivo enviado.")

    # Checked before anything is read or stored. `upload.size` comes from the
    # Content-Length Django already parsed, so this refuses before the bytes are
    # buffered rather than after.
    # `size` is Optional on the base class, and a None here would silently skip the cap
    # rather than refuse -- so an unknown size is refused outright.
    if upload.size is None or upload.size > settings.PORTAL_UPLOAD_MAX_BYTES:
        return HttpResponseBadRequest(
            f"Arquivo excede o limite de {settings.PORTAL_UPLOAD_MAX_BYTES} bytes.",
        )

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
    return redirect("portal-home")


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
