"""Workspace rendering kept separate from the cycle URL decorators."""

from uuid import UUID

from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import render

from apps.accounts.views import AuthenticatedRequest
from apps.authz.services import can
from apps.clients.maintenance import load_client
from apps.clients.models import ClientContact, Engagement, OnboardingItem
from apps.cycles.capabilities import CYCLES_OPERATE
from apps.cycles.models import Exception as CycleException
from apps.cycles.models import PlaybookVersion, ServiceCycle, VersionState
from apps.cycles.services.cycle import tenant_id
from apps.cycles.workspace import get_cycle_context, parse_period, timeline
from apps.obligations.models import Document, Obligation


def client_workspace(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Render the existing detail route after its auth and feature gates."""
    client = load_client(request.user, tenant_id(), pk)
    try:
        period = parse_period(request.GET.get("periodo"))
    except ValueError:
        return HttpResponse("Competência inválida. Use AAAA-MM.", status=422)
    # Imported here: cycle_views imports apps.clients.views, which imports this module.
    from apps.cycles.cycle_views import exception_rows  # noqa: PLC0415

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
                CycleException.objects.filter(
                    client=client, cycle__period_start=period
                ),
                client.pk,
            ),
            "engagements": engagements,
            "definitions": PlaybookVersion.objects.filter(state=VersionState.PUBLISHED)
            .select_related("playbook")
            .order_by("playbook_id", "-number")
            .distinct("playbook_id"),
            "may_operate": can(request.user, CYCLES_OPERATE, tenant_id=tenant_id()),
            "timeline": timeline(request.user, pk, period),
            "documents": Document.objects.filter(client=client).order_by(
                "-created_at", "pk"
            )[:20],
            "obligations": Obligation.objects.filter(
                client=client, competence_month=period
            ).select_related("obligation_type"),
            "contacts": ClientContact.objects.filter(client=client, active=True),
            "checklist": OnboardingItem.objects.filter(client=client).order_by(
                "position", "pk"
            ),
        },
    )
