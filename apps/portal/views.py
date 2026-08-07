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

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from apps.accounts.models import User
from apps.authz.services import require_can
from apps.clients.models import ClientCompany
from apps.obligations.models import Document, Obligation
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


@login_required
def portal_home(request: HttpRequest) -> HttpResponse:
    """Render the signed-in client's own company and its obligation count."""
    client = getattr(request, "client", None)
    # No client filter is applied on purpose. The RESTRICTIVE portal policy on
    # obligations_obligation compares app.client_id, so this count is already confined
    # to the one client — and writing `.filter(client=client)` here would make the test
    # that drops the policy pass anyway, hiding the very failure it exists to catch.
    obligation_count = Obligation.objects.count()
    return render(
        request,
        "portal/home.html",
        {"client": client, "obligation_count": obligation_count},
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
