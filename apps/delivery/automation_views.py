"""The firm's automation console, with scoped dead letters and versioned POSTs."""

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Exists, OuterRef
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from apps.accounts.views import AuthenticatedRequest
from apps.authz.portfolio import visible_clients
from apps.authz.services import require_can
from apps.clients.views import require_tenant
from apps.cycles.services.cycle import require_enabled, tenant_id
from apps.delivery.automations import cancel_dead_letter, retry_dead_letter
from apps.delivery.errors import VersionConflict
from apps.delivery.jobs import registered_jobs
from apps.delivery.models import AutomationPause, MessageState, OutboundMessage
from apps.delivery.services import AUTOMATIONS_MANAGE, set_pause


class AutomationForm(forms.Form):
    """Parse only the four manager actions and their rendered revision."""

    action = forms.ChoiceField(
        choices=[(value, value) for value in ("pause", "unpause", "retry", "cancel")]
    )
    expected_version = forms.IntegerField(min_value=1)
    message_id = forms.UUIDField(required=False)

    def clean(self) -> dict[str, object]:
        """Message actions must carry an id; tenant identity never comes from POST."""
        data = super().clean() or {}
        if data.get("action") in {"retry", "cancel"} and not data.get("message_id"):
            msg = "Selecione uma mensagem."
            raise ValidationError(msg)
        return data


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
@require_can(AUTOMATIONS_MANAGE)
def automations(request: AuthenticatedRequest) -> HttpResponse:
    """Read the effective schedule; every mutation repeats live locked authority."""
    require_enabled()
    error, status = "", 200
    if request.method == "POST":
        form = AutomationForm(request.POST)
        if not form.is_valid():
            error, status = "Revise a ação e a versão do registro.", 422
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
                    command = {
                        "retry": retry_dead_letter,
                        "cancel": cancel_dead_letter,
                    }[data["action"]]
                    command(
                        request.user,
                        data["message_id"],
                        expected_version=data["expected_version"],
                    )
            except VersionConflict as conflict:
                error, status = str(conflict), 409
            except ValidationError as invalid:
                error, status = " ".join(invalid.messages), 422
            else:
                messages.success(request, "Decisão registrada. Consulte a Comunicação.")
                return redirect("automations")
    clients = visible_clients(request.user, tenant_id=tenant_id())
    rows = (
        OutboundMessage.objects.filter(
            client_id__in=clients.values("pk"),
            state__in=[MessageState.REJECTED, MessageState.UNKNOWN],
        )
        .select_related("client")
        .annotate(
            has_retry=Exists(OutboundMessage.objects.filter(retry_of_id=OuterRef("pk")))
        )
        .order_by("created_at", "pk")
    )
    page = Paginator(rows, 30).get_page(request.GET.get("pagina"))
    return render(
        request,
        "delivery/automations.html",
        {
            "jobs": registered_jobs(),
            "page": page,
            "control": AutomationPause.objects.filter(tenant_id=tenant_id()).first(),
            "global_paused": settings.DELIVERY_PAUSED,
            "error": error,
        },
        status=status,
    )
