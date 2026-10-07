"""The Hoje page and its form actions, behind the briefing feature flag."""

from collections.abc import Mapping
from http import HTTPStatus
from urllib.parse import urlencode
from uuid import UUID

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.accounts.views import AuthenticatedRequest
from apps.authz.services import require_can
from apps.briefing.feedback import Dismissal, dismiss
from apps.briefing.forms import BriefingFilterForm, parse_selection, selection_values
from apps.briefing.presentation import briefing_view
from apps.briefing.providers import PROVIDER_LABELS
from apps.briefing.saved_views import delete_view, save_view
from apps.clients.views import require_tenant
from apps.cycles.errors import VersionConflict


class SaveViewForm(forms.Form):
    """Carry the named preference revision shown by the page."""

    name = forms.CharField(label="Nome da visualização", max_length=80)
    expected_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)


class DeleteViewForm(forms.Form):
    """A delete is bound to the revision it was offered for."""

    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class DismissalForm(forms.Form):
    """Parse identity and fact version, never a caller-supplied client attribution."""

    kind = forms.ChoiceField(choices=list(PROVIDER_LABELS.items()))
    subject_ref = forms.UUIDField()
    fact_version = forms.CharField(max_length=128)
    reason = forms.CharField(label="Motivo", max_length=500)
    fact_period = forms.DateField(input_formats=["%Y-%m"], required=False)


def _post_selection(request: AuthenticatedRequest) -> dict[str, str]:
    return {name: request.POST.get(name, "") for name in BriefingFilterForm.base_fields}


def _queue_url(values: Mapping[str, str]) -> str:
    scope, filters = parse_selection(values)
    params = {
        key: value for key, value in selection_values(scope, filters).items() if value
    }
    query = urlencode(params)
    return f"{reverse('dashboard')}?{query}" if query else reverse("dashboard")


@require_http_methods(["POST"])
@login_required
@require_tenant
@require_can("clients.view_assigned")
def save_view_action(request: AuthenticatedRequest) -> HttpResponse:
    """Save only parsed filter values with the form's optimistic revision."""
    values = _post_selection(request)
    form = SaveViewForm(request.POST)
    if not form.is_valid():
        return briefing_view(
            request,
            values=values,
            error=form.errors.as_text(),
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    try:
        saved = save_view(
            request.user,
            form.cleaned_data["name"],
            values,
            expected_version=form.cleaned_data["expected_version"],
        )
    except (VersionConflict, ValidationError) as invalid:
        status = (
            HTTPStatus.CONFLICT
            if isinstance(invalid, VersionConflict)
            else HTTPStatus.UNPROCESSABLE_ENTITY
        )
        return briefing_view(request, values=values, error=str(invalid), status=status)
    return redirect(f"{reverse('dashboard')}?{urlencode({'vista': str(saved.pk)})}")


@require_http_methods(["POST"])
@login_required
@require_tenant
@require_can("clients.view_assigned")
def delete_view_action(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Reject an old delete form instead of removing a newer preference."""
    values = _post_selection(request)
    form = DeleteViewForm(request.POST)
    if not form.is_valid():
        return briefing_view(
            request,
            values=values,
            error=form.errors.as_text(),
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    try:
        return_url = _queue_url(values)
        delete_view(
            request.user, pk, expected_version=form.cleaned_data["expected_version"]
        )
    except (VersionConflict, ValidationError) as invalid:
        status = (
            HTTPStatus.CONFLICT
            if isinstance(invalid, VersionConflict)
            else HTTPStatus.UNPROCESSABLE_ENTITY
        )
        return briefing_view(request, values=values, error=str(invalid), status=status)
    return redirect(return_url)


@require_http_methods(["POST"])
@login_required
@require_tenant
@require_can("clients.view_assigned")
def dismiss_action(request: AuthenticatedRequest) -> HttpResponse:
    """Record why this person hides this version, without changing the source fact."""
    values = _post_selection(request)
    form = DismissalForm(request.POST)
    if not form.is_valid():
        return briefing_view(
            request,
            values=values,
            error=form.errors.as_text(),
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    data = form.cleaned_data
    try:
        return_url = _queue_url(values)
        dismiss(
            request.user,
            Dismissal(
                data["kind"],
                data["subject_ref"],
                data["fact_version"],
                data["reason"],
                data["fact_period"],
            ),
        )
    except (VersionConflict, ValidationError) as invalid:
        status = (
            HTTPStatus.CONFLICT
            if isinstance(invalid, VersionConflict)
            else HTTPStatus.UNPROCESSABLE_ENTITY
        )
        return briefing_view(request, values=values, error=str(invalid), status=status)
    return redirect(return_url)
