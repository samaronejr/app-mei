"""Portfolio-scoped operational queue; every POST carries its rendered revision."""

from typing import TYPE_CHECKING

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from apps.accounts.views import AuthenticatedRequest
from apps.authz.portfolio import visible_clients
from apps.authz.services import can, require_can
from apps.clients.views import require_tenant
from apps.cycles.capabilities import CYCLES_OPERATE
from apps.cycles.services.cycle import require_enabled, tenant_id
from apps.delivery.errors import VersionConflict
from apps.delivery.models import (
    AutomationPause,
    MessageState,
    OutboundMessage,
)
from apps.delivery.services import (
    AUTOMATIONS_MANAGE,
    decide,
    remaining_quota,
    set_pause,
)
from apps.delivery.timeline import message_history

if TYPE_CHECKING:
    from uuid import UUID


class DecisionForm(forms.Form):
    """Only registered decisions, not arbitrary desired states."""

    message_id = forms.UUIDField(required=False)
    expected_version = forms.IntegerField(min_value=1)
    action = forms.ChoiceField(
        choices=[
            (value, value)
            for value in (
                "cancel",
                "sent",
                "not_sent",
                "pause",
                "unpause",
            )
        ]
    )
    note = forms.CharField(required=False, max_length=10000)

    def clean(self) -> dict[str, object]:
        """Require a scoped message id except for tenant-level pause controls."""
        data = super().clean() or {}
        if data.get("action") not in {"pause", "unpause"} and not data.get(
            "message_id"
        ):
            msg = "Selecione uma mensagem."
            raise ValidationError(msg)
        return data


class MessageSelectionForm(forms.Form):
    """A present selector names one UUID; an empty selector is not a queue filter."""

    mensagem = forms.UUIDField()


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
@require_can("clients.view_assigned")
def communication(request: AuthenticatedRequest) -> HttpResponse:
    """Queue, failures and human uncertainty decisions without evidence inflation."""
    require_enabled()
    selector = MessageSelectionForm(request.GET) if "mensagem" in request.GET else None
    if selector is not None and not selector.is_valid():
        raise Http404
    message_id: UUID | None = (
        selector.cleaned_data["mensagem"] if selector is not None else None
    )
    error, status = "", 200
    if request.method == "POST":
        form = DecisionForm(request.POST)
        if not form.is_valid():
            error, status = "Revise os dados informados.", 422
        else:
            data = form.cleaned_data
            try:
                if data["action"] in {"pause", "unpause"}:
                    set_pause(
                        request.user,
                        paused=data["action"] == "pause",
                        expected_version=data["expected_version"],
                    )
                else:
                    decide(
                        request.user,
                        data["message_id"],
                        action=data["action"],
                        note=data["note"],
                        expected_version=data["expected_version"],
                    )
            except VersionConflict as conflict:
                error, status = str(conflict), 409
            except ValidationError as invalid:
                error, status = " ".join(invalid.messages), 422
            else:
                return redirect("delivery-queue")
    clients = visible_clients(request.user, tenant_id=tenant_id())
    rows = OutboundMessage.objects.filter(
        client_id__in=clients.values("pk")
    ).select_related("client")
    if message_id is not None:
        get_object_or_404(rows, pk=message_id)
        rows = rows.filter(pk=message_id)
    selected = "" if message_id is not None else request.GET.get("estado", "")
    if selected:
        rows = rows.filter(state=selected)
    page = Paginator(rows.order_by("-created_at", "pk"), 30).get_page(
        request.GET.get("pagina")
    )
    control = AutomationPause.objects.filter(tenant_id=tenant_id()).first()
    return render(
        request,
        "delivery/queue.html",
        {
            "page": page,
            "communications": message_history(list(page.object_list)),
            "states": MessageState.choices,
            "selected": selected,
            "selected_message": message_id,
            "error": error,
            "control": control,
            "quota": remaining_quota(),
            "can_decide": can(request.user, CYCLES_OPERATE, tenant_id=tenant_id()),
            "can_pause": can(request.user, AUTOMATIONS_MANAGE, tenant_id=tenant_id()),
        },
        status=status,
    )
