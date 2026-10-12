"""Workspace rendering kept separate from the cycle URL decorators."""

from typing import Final
from uuid import UUID

from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import render

from apps.accounts.views import AuthenticatedRequest
from apps.authz.services import can
from apps.clients.maintenance import MANAGE_CAPABILITY, load_client
from apps.clients.models import ClientContact, Engagement, OnboardingItem
from apps.core.flags import tenant_has
from apps.cycles.capabilities import CYCLES_OPERATE
from apps.cycles.exception_context import exception_rows
from apps.cycles.models import Exception as CycleException
from apps.cycles.models import PlaybookVersion, ServiceCycle, VersionState
from apps.cycles.services.cycle import tenant_id
from apps.cycles.workspace import get_cycle_context, parse_period, timeline
from apps.obligations.coverage import coverage_for
from apps.obligations.models import Document, Obligation

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
    client = load_client(request.user, tenant_id(), pk)
    try:
        period = (
            selected_exception.cycle.period_start
            if selected_exception is not None
            else parse_period(request.GET.get("periodo"))
        )
    except ValueError:
        return HttpResponse("Competência inválida. Use AAAA-MM.", status=422)
    context = get_cycle_context(request.user, pk, period)
    cycles = (
        ServiceCycle.objects.filter(client=client, period_start=period)
        .select_related("playbook", "owner", "engagement")
        .order_by("created_at", "pk")
    )
    engagements = (
        Engagement.objects.filter(client=client, starts_on__lte=period)
        .filter(Q(ends_on__isnull=True) | Q(ends_on__gt=period))
        .select_related("responsible")
    )
    return render(
        request,
        "cycles/workspace.html",
        {
            "client": client,
            "period": period,
            "cycle_context": context,
            "cycles": cycles,
            "exceptions": exception_rows(
                request,
                CycleException.objects.filter(
                    client=client, cycle__period_start=period
                ),
                "cliente",
                selected_exception,
                error,
            ),
            "engagements": engagements,
            "definitions": PlaybookVersion.objects.filter(state=VersionState.PUBLISHED)
            .select_related("playbook")
            .order_by("playbook_id", "-number")
            .distinct("playbook_id"),
            "may_operate": can(request.user, CYCLES_OPERATE, tenant_id=tenant_id()),
            # AXR1-047: whether to draw the way into this client's setup page.
            # The route behind it answers 404 without `import_r1` and 403 without
            # `clients.manage` (`apps/clients/views_onboarding.py:412-424`), so the
            # link is offered exactly when both already say yes — the decorators
            # there remain the authorization, this only keeps the page honest about
            # what it offers. A read-only support session resolves `clients.manage`
            # to NONE and is therefore shown no link it would be refused.
            "may_set_up": tenant_has(tenant_id(), SETUP_FLAG)
            and can(request.user, MANAGE_CAPABILITY, tenant_id=tenant_id()),
            "timeline": timeline(request.user, pk, period),
            "documents": Document.objects.filter(client=client).order_by(
                "-created_at", "pk"
            )[:20],
            "obligations": Obligation.objects.filter(
                client=client, competence_month=period
            ).select_related("obligation_type"),
            # T17: the Cobertura panel — what the firm knows about each
            # engagement's obligations this competence, honest states only.
            "coverage": coverage_for(request.user, client, period),
            "contacts": ClientContact.objects.filter(client=client, active=True),
            "checklist": OnboardingItem.objects.filter(client=client).order_by(
                "position", "pk"
            ),
        },
        status=status,
    )
