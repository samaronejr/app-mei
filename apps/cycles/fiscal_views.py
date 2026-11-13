"""The fiscal workspace behind the existing firm document review route."""

from http import HTTPStatus
from uuid import UUID

from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Prefetch
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.accounts.views import AuthenticatedRequest
from apps.authz.objects import load_visible_client
from apps.authz.support import in_support_mode
from apps.core.flags import tenant_has
from apps.cycles.errors import VersionConflict
from apps.cycles.fiscal_records import FiscalDocumentReview
from apps.cycles.records import ServiceCycle
from apps.cycles.services.cycle import tenant_id
from apps.cycles.services.fiscal_review import (
    DECISIONS,
    NOTE_MAX_LENGTH,
    input_digest,
    record_fiscal_document_review,
)
from apps.obligations.models import FiscalDocument, FiscalRevision, RevisionArtifact


class FiscalReviewForm(forms.Form):
    """Only explicit identifiers, a pinned input and the accountant's decision."""

    cycle_id = forms.UUIDField()
    revision_id = forms.UUIDField()
    expected_version = forms.IntegerField(min_value=1)
    input_digest = forms.RegexField(r"^[a-f0-9]{64}$")
    decision = forms.ChoiceField(choices=DECISIONS)
    note = forms.CharField(required=False, max_length=NOTE_MAX_LENGTH)


def fiscal_document_review(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Reuse the document route's authorization, then recheck the fiscal scope."""
    if not tenant_has(tenant_id(), "document_capture"):
        raise Http404
    fiscal = get_object_or_404(
        FiscalDocument.objects.select_related("client", "current_revision"), pk=pk
    )
    load_visible_client(request.user, fiscal.client_id)
    error, status = "", HTTPStatus.OK
    if request.method == "POST":
        if in_support_mode():
            raise PermissionDenied
        form = FiscalReviewForm(request.POST)
        if not form.is_valid():
            error, status = (
                "Revise os dados da decisão.",
                HTTPStatus.UNPROCESSABLE_CONTENT,
            )
        else:
            data = form.cleaned_data
            try:
                record_fiscal_document_review(
                    request.user,
                    data["cycle_id"],
                    fiscal.pk,
                    revision_id=data["revision_id"],
                    decision=data["decision"],
                    input_digest=data["input_digest"],
                    expected_version=data["expected_version"],
                    note=data["note"],
                )
            except VersionConflict as conflict:
                error, status = str(conflict), HTTPStatus.CONFLICT
            except ValidationError as invalid:
                error, status = (
                    " ".join(invalid.messages),
                    HTTPStatus.UNPROCESSABLE_CONTENT,
                )
            else:
                return redirect(f"{reverse('document-queue')}?fila=fiscais")
        fiscal.refresh_from_db()
    period = fiscal.issue_date if fiscal.family == "nfe" else fiscal.competence
    cycles = (
        list(
            ServiceCycle.objects.filter(
                client_id=fiscal.client_id,
                period_start__lte=period,
                period_end__gte=period,
            ).order_by("period_start", "pk")
        )
        if period is not None and fiscal.role_verification != "divergent"
        else []
    )
    selected = next(
        (cycle for cycle in cycles if str(cycle.pk) == request.GET.get("ciclo")),
        cycles[0] if cycles else None,
    )
    reviews = list(
        FiscalDocumentReview.objects.filter(document=fiscal)
        .select_related("reviewer", "cycle")
        .order_by("reviewed_at", "pk")
    )
    decided = any(
        review.revision_id == fiscal.current_revision_id
        and selected is not None
        and review.cycle_id == selected.pk
        for review in reviews
    )
    revisions = (
        FiscalRevision.objects.filter(document=fiscal)
        .prefetch_related(
            Prefetch(
                "artifacts",
                queryset=RevisionArtifact.objects.select_related("document").order_by(
                    "role", "pk"
                ),
            )
        )
        .order_by("-observed_at", "-pk")
    )
    return render(
        request,
        "cycles/fiscal_document_review.html",
        {
            "fiscal": fiscal,
            "cycle": selected,
            "cycles": cycles,
            "revisions": revisions,
            "reviews": reviews,
            "decided": decided,
            "input_digest": input_digest(fiscal),
            "decisions": DECISIONS,
            "error": error,
        },
        status=status,
    )
