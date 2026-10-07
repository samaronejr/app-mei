"""Deterministic active portfolios for the briefing budget and later scale work."""

from datetime import timedelta
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from apps.clients.models import (
    ClientCompany,
    ClientStatus,
    Engagement,
    TaxProfile,
    TaxRegime,
)
from apps.clients.models.engagement import ServiceKind
from apps.core.tenancy import tenant_context
from apps.obligations.models import Obligation, ObligationStatus
from tests.briefing.fixtures import NOW
from tests.ui.factories import Firm, make_cnpj, make_firm


def build_firm(n_clients: int, seed: int) -> Firm:
    """Build active, scoped clients with real profiles, engagements and due facts."""
    if not 0 <= n_clients <= 1_000_000:
        message = "The fixture supports between zero and one million clients."
        raise ValueError(message)
    firm = make_firm(f"briefing-scale-{seed}")
    firm.tenant.features = ["cycles_r1", "briefing_r1"]
    firm.tenant.save(update_fields=["features"])
    prefix = "A" + sha256(str(seed).encode()).hexdigest()[:5].upper()
    period = NOW.date().replace(day=1)
    with tenant_context(firm.tenant.pk):
        clients = ClientCompany.objects.bulk_create(
            [
                ClientCompany(
                    id=uuid5(NAMESPACE_URL, f"axorim-scale:{seed}:client:{index}"),
                    tenant=firm.tenant,
                    cnpj=make_cnpj(f"{prefix}{index:06d}"),
                    legal_name=f"Cliente de escala {seed} — {index:04d}",
                    status=ClientStatus.ACTIVE,
                    state="SP",
                    municipality_ibge_code="3550308",
                )
                for index in range(n_clients)
            ]
        )
        TaxProfile.objects.bulk_create(
            [
                TaxProfile(
                    tenant=firm.tenant,
                    client=client,
                    regime=TaxRegime.MEI,
                    valid_from=period,
                    provenance="staff_entry",
                )
                for client in clients
            ]
        )
        Engagement.objects.bulk_create(
            [
                Engagement(
                    tenant=firm.tenant,
                    client=client,
                    service_kind=ServiceKind.MEI_MENSAL,
                    starts_on=period,
                    responsible=firm.owner,
                    provenance="staff_entry",
                )
                for client in clients
            ]
        )
        Obligation.objects.bulk_create(
            [
                Obligation(
                    tenant=firm.tenant,
                    client=client,
                    obligation_type_id="DAS",
                    competence_month=period,
                    nominal_due_date=NOW.date() + timedelta(days=2),
                    resolved_due_date=NOW.date() + timedelta(days=2),
                    status=ObligationStatus.DUE,
                )
                for client in clients
            ]
        )
    firm.clients.extend(clients)
    return firm
