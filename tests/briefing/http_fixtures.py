"""A real signed-in firm with one actionable fact for HTTP contracts."""

from datetime import timedelta

from apps.clients.models import ClientAssignment
from apps.core.tenancy import tenant_context
from apps.obligations.models import Obligation, ObligationStatus
from tests.briefing.fixtures import NOW
from tests.ui.factories import Firm, make_firm


def http_firm() -> Firm:
    firm = make_firm("briefing-http", client_count=1)
    firm.tenant.features = ["cycles_r1", "briefing_r1"]
    firm.tenant.save(update_fields=["features"])
    with tenant_context(firm.tenant.pk):
        ClientAssignment.objects.create(
            tenant=firm.tenant, client=firm.clients[0], user=firm.accountant
        )
        subject = Obligation.objects.create(
            tenant=firm.tenant,
            client=firm.clients[0],
            obligation_type_id="DAS",
            competence_month=NOW.date().replace(day=1),
            nominal_due_date=NOW.date() + timedelta(days=2),
            resolved_due_date=NOW.date() + timedelta(days=2),
            status=ObligationStatus.DUE,
        )
        Obligation.objects.filter(pk=subject.pk).update(
            created_at=NOW - timedelta(days=2), updated_at=NOW - timedelta(days=1)
        )
    return firm
