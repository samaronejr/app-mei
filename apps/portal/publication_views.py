"""Client-scoped base-table reads and INSERT-only exact-version receipts."""

from uuid import UUID

from django import forms
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods
from psycopg.errors import UniqueViolation

from apps.authz.services import require_can
from apps.cycles.models import (
    ClientAcknowledgement,
    ClientPublication,
    CycleEvent,
    EvidenceLink,
    PublicationState,
    ReviewDecision,
)
from apps.portal.request_views import _require_flag
from apps.portal.views import PortalVaultRequest, document_download


class ReceiptForm(forms.Form):
    """The POST names the exact public content the person saw."""

    publication_digest = forms.RegexField(r"^[0-9a-f]{64}$", max_length=64)


@require_http_methods(["GET", "HEAD", "POST"])
@login_required
@require_can("publications.acknowledge")
def publication_detail(request: PortalVaultRequest, pk: UUID) -> HttpResponse:
    """Record a view as an event and require the explicit digest POST for receipt."""
    _require_flag(request)
    publication = get_object_or_404(ClientPublication.objects, pk=pk)
    published = publication.state == PublicationState.PUBLISHED
    error, status = "", 200
    receipts = request.session.get("publication_receipts", {})
    acknowledged = receipts.get(str(pk)) == publication.publication_digest
    if request.method == "POST":
        form = ReceiptForm(request.POST)
        if not form.is_valid():
            error, status = "Recarregue a publicação antes de confirmar.", 422
        elif not published:
            error, status = "Esta publicação foi retirada pelo escritório.", 409
        elif form.cleaned_data["publication_digest"] != publication.publication_digest:
            error, status = "A versão informada não corresponde à publicação.", 409
        elif not acknowledged:
            # App-supplied UUID/defaults need no SELECT on this INSERT-only table.
            # Reuse this session's own prior receipt, but announce a DB conflict.
            try:
                with transaction.atomic():
                    ClientAcknowledgement.objects.bulk_create(
                        [
                            ClientAcknowledgement(
                                tenant_id=publication.tenant_id,
                                client_id=publication.client_id,
                                publication=publication,
                                user=request.user,
                                publication_digest=publication.publication_digest,
                            )
                        ]
                    )
            except IntegrityError as conflict:
                if (
                    not isinstance(conflict.__cause__, UniqueViolation)
                    or conflict.__cause__.diag.constraint_name
                    != "acknowledgement_person_uniq"
                ):
                    raise
                error, status = "Este recebimento já foi registrado.", 409
            receipts[str(pk)] = publication.publication_digest
            request.session["publication_receipts"] = receipts
            acknowledged = True
    elif request.method == "GET" and not any(
        "prefetch" in request.headers.get(header, "").lower()
        or "prerender" in request.headers.get(header, "").lower()
        for header in ("Purpose", "Sec-Purpose", "X-Moz")
    ):
        CycleEvent.objects.create(
            tenant_id=publication.tenant_id,
            client_id=publication.client_id,
            cycle_id=publication.cycle_id,
            actor=request.user,
            type="publication.viewed",
            aggregate_type="ClientPublication",
            aggregate_id=publication.pk,
            aggregate_version=publication.version,
            payload={"digest": publication.publication_digest},
        )
    links = (
        EvidenceLink.objects.filter(
            pk__in=[item["link_id"] for item in publication.documents]
        )
        .select_related("document")
        .order_by("pk")
        if published
        else []
    )
    return render(
        request,
        "portal/publication.html",
        {
            "cycles_r1": True,
            "publication": {
                "id": publication.pk,
                "title": publication.title,
                "body": publication.body_ptbr,
                "digest": publication.publication_digest,
                "published_at": publication.published_at,
                "withdrawn_at": publication.withdrawn_at,
                "published": published,
            },
            "documents": [
                {"id": link.pk, "name": link.document.original_filename}
                for link in links
            ],
            "acknowledged": acknowledged,
            "error": error,
        },
        status=status,
    )


@require_http_methods(["GET"])
@login_required
@require_can("documents.transfer")
def publication_download(
    request: PortalVaultRequest, pk: UUID, link_id: UUID
) -> HttpResponse:
    """Authorize the exact link revision before the existing safe storage adapter."""
    _require_flag(request)
    publication = get_object_or_404(
        ClientPublication.objects, pk=pk, state=PublicationState.PUBLISHED
    )
    selected = next(
        (row for row in publication.documents if row["link_id"] == str(link_id)),
        None,
    )
    if selected is None:
        raise Http404
    link = get_object_or_404(
        EvidenceLink.objects.select_related("document"),
        pk=link_id,
        request__cycle_id=publication.cycle_id,
        version=selected["link_revision"],
        document_id=selected["document_id"],
        document__sha256=selected["sha256"],
        document__byte_size=selected["size"],
        review_decision=ReviewDecision.ACCEPTED,
    )
    return document_download(request, link.document.storage_key)
