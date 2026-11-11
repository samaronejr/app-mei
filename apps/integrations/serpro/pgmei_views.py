"""Staff action "Obter guia DAS oficial" on an MEI cycle (AXRM-017)."""

from http import HTTPStatus
from uuid import UUID

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_http_methods

from apps.accounts.views import AuthenticatedRequest
from apps.authz.services import require_can
from apps.clients.views import require_tenant
from apps.cycles.cycle_views import _render_detail
from apps.cycles.services.cycle import load_cycle, tenant_id
from apps.integrations.serpro.pgmei import (
    CAPABILITY,
    REFUSAL_LABELS,
    guide_eligibility,
)
from apps.integrations.tasks import obtain_das_guide_task


@require_http_methods(["POST"])
@login_required
@require_tenant
@require_can(CAPABILITY)
def cycle_das_guide(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Queue the guide after this request commits; the task re-checks everything."""
    cycle = load_cycle(request.user, pk)
    reason = guide_eligibility(request.user, cycle)
    if reason:
        return _render_detail(
            request, cycle.pk, REFUSAL_LABELS[reason], HTTPStatus.CONFLICT
        )
    arguments = (str(tenant_id()), str(request.user.pk), str(cycle.pk))
    transaction.on_commit(lambda: obtain_das_guide_task.delay(*arguments))
    return redirect("cycle-detail", pk=cycle.pk)
