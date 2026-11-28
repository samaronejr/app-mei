"""The fiscal workspace behind the existing firm document review route."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from apps.authz.objects import load_visible_client
from apps.authz.support import in_support_mode
from apps.core.flags import tenant_has
from apps.core.refusal import WayBack, refusal_page
from apps.cycles.errors import IllegalTransition, VersionConflict
from apps.cycles.fiscal_cycle_input import INVALID_SCOPE, submitted_cycle
from apps.cycles.fiscal_preview import preview_context
from apps.cycles.fiscal_records import DECISIONS
from apps.cycles.fiscal_snapshot import load_snapshot
from apps.cycles.services.cycle import tenant_id
from apps.cycles.services.fiscal_review import (
    NOTE_MAX_LENGTH,
    record_fiscal_document_review,
)

if TYPE_CHECKING:
    from uuid import UUID

    from apps.accounts.views import AuthenticatedRequest


class FiscalReviewForm(forms.Form):
    """Only explicit identifiers, a pinned input and the accountant's decision."""

    revision_id = forms.UUIDField()
    expected_version = forms.IntegerField(min_value=1)
    expected_head_id = forms.UUIDField(required=False)
    input_digest = forms.RegexField(r"^[a-f0-9]{64}$")
    decision = forms.ChoiceField(choices=DECISIONS)
    # The locked writer applies the whitespace contract shared with the database.
    note = forms.CharField(required=False, max_length=NOTE_MAX_LENGTH, strip=False)


def fiscal_document_review(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Reuse the document route's authorization, then recheck the fiscal scope."""
    if not tenant_has(tenant_id(), "document_capture"):
        raise Http404
    fiscal = load_snapshot(pk)
    load_visible_client(request.user, fiscal.client_id)
    if request.method == "POST" and in_support_mode():
        raise PermissionDenied
    try:
        requested_cycle = submitted_cycle(request)
    except ValidationError:
        return refusal_page(
            request,
            HTTPStatus.UNPROCESSABLE_CONTENT,
            heading="Revisar documento fiscal",
            reason=INVALID_SCOPE,
            back=WayBack(
                f"{reverse('document-queue')}?fila=fiscais",
                "Voltar para documentos fiscais",
            ),
        )
    error, status = "", HTTPStatus.OK
    if request.method == "POST":
        if requested_cycle is None:
            raise Http404
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
                    requested_cycle,
                    fiscal.pk,
                    revision_id=data["revision_id"],
                    decision=data["decision"],
                    input_digest=data["input_digest"],
                    expected_version=data["expected_version"],
                    expected_head_id=data["expected_head_id"],
                    note=data["note"],
                )
            except (IllegalTransition, VersionConflict) as conflict:
                error, status = str(conflict), HTTPStatus.CONFLICT
            except ValidationError as invalid:
                error, status = (
                    " ".join(invalid.messages),
                    HTTPStatus.UNPROCESSABLE_CONTENT,
                )
            else:
                return redirect(f"{reverse('document-queue')}?fila=fiscais")
        fiscal = load_snapshot(pk)
    return render(
        request,
        "cycles/fiscal_document_review.html",
        {
            "fiscal": fiscal,
            **preview_context(fiscal, requested_cycle),
            "decisions": DECISIONS,
            "error": error,
        },
        status=status,
    )
