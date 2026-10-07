"""Render Hoje without importing action controllers or the legacy dashboard."""

from collections.abc import Mapping
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import urlencode
from uuid import UUID

from django import forms
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import render

from apps.accounts.views import AuthenticatedRequest
from apps.briefing.forms import BriefingFilterForm, selection_values
from apps.briefing.providers import PROVIDER_LABELS
from apps.briefing.queries import briefing
from apps.briefing.saved_views import resolve_view
from apps.cycles.models import SavedView

if TYPE_CHECKING:
    from apps.briefing.types import BriefingPage


def briefing_view(
    request: AuthenticatedRequest,
    *,
    error: str = "",
    status: HTTPStatus = HTTPStatus.OK,
    values: Mapping[str, str] | None = None,
) -> HttpResponse:
    """Render from the dashboard's existing login, tenant and capability boundary."""
    saved_views = list(
        SavedView.objects.filter(user=request.user).order_by("name", "pk")
    )
    selected: SavedView | None = None
    data = (
        dict(values)
        if values is not None
        else {key: request.GET.get(key, "") for key in BriefingFilterForm.base_fields}
    )
    page: BriefingPage | None = None
    form = BriefingFilterForm(data)
    try:
        if values is None and request.GET.get("vista"):
            try:
                view_id = UUID(request.GET["vista"])
            except ValueError as invalid:
                raise Http404 from invalid
            scope, filters = resolve_view(request.user, view_id)
            data = selection_values(scope, filters)
            selected = next((view for view in saved_views if view.pk == view_id), None)
            form = BriefingFilterForm(data)
        scope, filters = form.selection()
        page = briefing(
            request.user,
            scope=scope,
            filters=filters,
            cursor=request.GET.get("cursor") if values is None else None,
        )
        data = selection_values(scope, filters)
    except ValidationError as invalid:
        error = " ".join(invalid.messages)
        status = HTTPStatus.UNPROCESSABLE_ENTITY
    all_failed = page is not None and len(page.failed_providers) == len(PROVIDER_LABELS)
    if all_failed and status == HTTPStatus.OK:
        status = HTTPStatus.SERVICE_UNAVAILABLE
    owners = (
        [(str(identifier), label) for identifier, label in page.owners] if page else []
    )
    form.fields["responsavel"].widget = forms.Select(
        choices=[("", "Todos os responsáveis"), *owners]
    )
    params = {key: value for key, value in data.items() if value}
    filter_query = urlencode(params)
    next_query = (
        urlencode(params | {"cursor": page.next_cursor})
        if page is not None and page.next_cursor
        else ""
    )
    return render(
        request,
        "briefing/today.html",
        {
            "page": page,
            "filter_form": form,
            "filter_values": data,
            "filter_query": filter_query,
            "next_query": next_query,
            "saved_views": saved_views,
            "selected_view": selected,
            "error": error,
            "all_providers_failed": all_failed,
            "failed_provider_labels": (
                [PROVIDER_LABELS[kind] for kind in page.failed_providers]
                if page
                else []
            ),
        },
        status=status,
    )
