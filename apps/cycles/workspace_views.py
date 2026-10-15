"""Workspace rendering kept separate from the cycle URL decorators."""

from http import HTTPStatus
from typing import Final
from uuid import UUID

from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse

from apps.accounts.views import AuthenticatedRequest
from apps.clients.maintenance import MANAGE_CAPABILITY, load_client
from apps.core.flags import tenant_has
from apps.core.refusal import WayBack, refusal_page
from apps.cycles.capabilities import CYCLES_OPERATE
from apps.cycles.exception_context import (
    exception_selection,
    present_exceptions,
    read_exceptions,
)
from apps.cycles.models import Exception as CycleException
from apps.cycles.services.cycle import tenant_id
from apps.cycles.workspace import parse_period
from apps.cycles.workspace_queries import read_workspace

# AXR1-047: the flag the setup route is gated on, written as a literal for the
# reason `cycle_views` writes `"bulk_r1"` and `clients.views` writes `"cycles_r1"`
# — `apps.clients.views` imports this module, so importing its `ONBOARDING_FLAG`
# back would close an import cycle. A misspelling is not silent: `tenant_has`
# refuses a slug outside `FEATURES` instead of answering False.
SETUP_FLAG: Final = "import_r1"


def client_workspace(
    request: AuthenticatedRequest,
    pk: UUID,
    selected_exception: CycleException | None = None,
    error: str = "",
    status: int = 200,
) -> HttpResponse:
    """Render the existing detail route after its auth and feature gates."""
    try:
        period = (
            selected_exception.cycle.period_start
            if selected_exception is not None
            else parse_period(request.GET.get("periodo"))
        )
    except ValueError:
        # Preserve the portfolio-first refusal even for malformed periods.
        load_client(request.user, tenant_id(), pk)
        return refusal_page(
            request,
            HTTPStatus.UNPROCESSABLE_CONTENT,
            heading="Não foi possível abrir o espaço do cliente",
            reason="Competência inválida. Use AAAA-MM.",
            back=WayBack(
                reverse("client-detail", args=[pk]), "Voltar ao espaço do cliente"
            ),
        )
    workspace = read_workspace(request.user, pk, period)
    selection = exception_selection(request, selected_exception, error)
    exceptions = read_exceptions(
        CycleException.objects.filter(client_id=pk, cycle__period_start=period),
        selection,
    )
    return render(
        request,
        "cycles/workspace.html",
        {
            "client": workspace.client,
            "period": period,
            "cycle_context": workspace.context,
            "cycles": workspace.cycles,
            "exceptions": present_exceptions(
                exceptions,
                selection,
                workspace.exception_controls,
                "cliente",
            ),
            "engagements": workspace.engagements,
            "definitions": workspace.definitions,
            "may_operate": CYCLES_OPERATE in workspace.allowed,
            # AXR1-047: whether to draw the way into this client's setup page.
            # The route behind it answers 404 without `import_r1` and 403 without
            # `clients.manage` (`apps/clients/views_onboarding.py:412-424`), so the
            # link is offered exactly when both already say yes — the decorators
            # there remain the authorization, this only keeps the page honest about
            # what it offers. A read-only support session resolves `clients.manage`
            # to NONE and is therefore shown no link it would be refused.
            "may_set_up": tenant_has(tenant_id(), SETUP_FLAG)
            and MANAGE_CAPABILITY in workspace.allowed,
            "timeline": workspace.timeline,
            # AXR1-043: the linked request and communication sections, read once
            # for the client this page already resolved through its portfolio.
            "period_requests": workspace.requests,
            "communication": workspace.communication,
            "documents": workspace.documents,
            "obligations": workspace.obligations,
            # T17: the Cobertura panel — what the firm knows about each
            # engagement's obligations this competence, honest states only.
            "coverage": workspace.coverage,
            "contacts": workspace.contacts,
            "checklist": workspace.checklist,
        },
        status=status,
    )
