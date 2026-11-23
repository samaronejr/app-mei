"""Client-scoped base-table reads and INSERT-only exact-version receipts."""

from uuid import UUID

from django import forms
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, connection, transaction
from django.db.models import UUIDField
from django.db.models.expressions import RawSQL
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods
from psycopg.errors import UniqueViolation

from apps.authz.services import require_can
from apps.cycles.models import (
    ClientAcknowledgement,
    ClientPublication,
    CycleEvent,
    PublicationState,
)
from apps.portal.request_views import _require_flag
from apps.portal.views import PortalVaultRequest, servable_documents, serve_document


def _documents(publication: ClientPublication) -> list[dict[str, str | UUID]]:
    if not publication.documents or publication.state != PublicationState.PUBLISHED:
        return []
    # Keep ordinary links as visible under their own RLS. For hidden links only
    # the exact approved document ID crosses the boundary; names come from RLS.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT selected.link_id, document.original_filename
            FROM pg_catalog.unnest(%s::uuid[]) AS selected(link_id)
            LEFT JOIN public.cycles_evidencelink AS visible
              ON visible.id = selected.link_id
            JOIN public.obligations_document AS document
              ON document.id = COALESCE(
                  visible.document_id,
                  public.cycles_portal_publication_document(%s, selected.link_id)
              )
            ORDER BY selected.link_id
            """,
            [[item["link_id"] for item in publication.documents], publication.pk],
        )
        return [{"id": link_id, "name": name} for link_id, name in cursor.fetchall()]


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
    revision = ClientPublication.objects.filter(supersedes=publication).first()
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
                "revision_id": revision.pk if revision else None,
            },
            "documents": _documents(publication),
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
    document_id = RawSQL(
        "public.cycles_portal_publication_document(%s, %s)",
        (pk, link_id),
        output_field=UUIDField(),
    )
    document = get_object_or_404(servable_documents(request), pk=document_id)
    return serve_document(document)
