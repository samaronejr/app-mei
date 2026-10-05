"""The firm proposes a withdrawal; this view never withdraws content directly."""

from uuid import UUID

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from apps.accounts.views import AuthenticatedRequest
from apps.authz.services import require_can
from apps.clients.maintenance import load_client
from apps.clients.views import require_tenant
from apps.cycles.effects.build import BuildError
from apps.cycles.errors import IllegalTransition, VersionConflict
from apps.cycles.models import ClientPublication
from apps.cycles.services.cycle import require_enabled, tenant_id
from apps.cycles.services.publications import propose_withdrawal


class WithdrawalForm(forms.Form):
    """Carry the publication version the operator actually reviewed."""

    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    reason = forms.CharField(
        label="Motivo da retirada",
        max_length=2000,
        widget=forms.Textarea(attrs={"class": "ui-field-input", "rows": 4}),
    )


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
@require_can("cycles.operate")
def publication_withdraw(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Create the new exact-effect card for an independently approved withdrawal."""
    require_enabled()
    publication = get_object_or_404(ClientPublication.objects, pk=pk)
    load_client(request.user, tenant_id(), publication.client_id)
    form = WithdrawalForm(
        request.POST if request.method == "POST" else None,
        initial={"expected_version": publication.version},
    )
    status = 200
    if request.method == "POST":
        if not form.is_valid():
            status = 422
        else:
            try:
                proposal = propose_withdrawal(
                    request.user, publication.pk, **form.cleaned_data
                )
            except VersionConflict as error:
                form.add_error(None, str(error))
                status = 409
            except (ValidationError, IllegalTransition, BuildError) as error:
                form.add_error(None, str(error))
                status = 422
            else:
                return redirect("approval-card", pk=proposal.pk)
    return render(
        request,
        "cycles/publication_withdraw.html",
        {"publication": publication, "form": form},
        status=status,
    )
