"""Deterministic active portfolios shared by scale tests and the local demo seed."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from typing import Final
from uuid import NAMESPACE_URL, UUID, uuid5

from apps.accounts.models import User
from apps.clients.models import (
    ClientCompany,
    ClientStatus,
    Engagement,
    TaxProfile,
    TaxRegime,
)
from apps.clients.models.engagement import ServiceKind
from apps.core.tenancy import tenant_context
from apps.fiscal.validators import cnpj_check_digits
from apps.obligations.models import Obligation, ObligationStatus
from apps.tenants.models import Tenant

DEMO_NOW: Final = datetime(2026, 9, 20, 12, tzinfo=UTC)
MAX_CLIENTS: Final = 1_000_000
HISTORY_START: Final = date(2025, 10, 1)


@dataclass(frozen=True, slots=True)
class ScaleSpec:
    """The seed and fixed clock are explicit, including in the existing scale test."""

    n_clients: int
    seed: int
    now: datetime = DEMO_NOW

    def __post_init__(self) -> None:
        """Reject invalid sizes before the caller creates a tenant."""
        if not 0 <= self.n_clients <= MAX_CLIENTS:
            message = "The fixture supports between zero and one million clients."
            raise ValueError(message)


def demo_id(seed: int, key: str) -> UUID:
    """Derive stable synthetic identities without using a process-global RNG."""
    return uuid5(NAMESPACE_URL, f"axorim-scale:{seed}:{key}")


def build_portfolio(
    tenant: Tenant, owner: User, spec: ScaleSpec
) -> list[ClientCompany]:
    """Create the T19 active graph; callers own tenant creation and transactions."""
    prefix = "A" + sha256(str(spec.seed).encode()).hexdigest()[:5].upper()
    period = spec.now.date().replace(day=1)
    with tenant_context(tenant.pk):
        clients = ClientCompany.objects.bulk_create(
            [
                ClientCompany(
                    id=demo_id(spec.seed, f"client:{index}"),
                    tenant=tenant,
                    cnpj=(base := f"{prefix}{index:06d}") + cnpj_check_digits(base),
                    legal_name=f"Cliente de escala {spec.seed} — {index:04d}",
                    status=ClientStatus.ACTIVE,
                    state="SP",
                    municipality_ibge_code="3550308",
                )
                for index in range(spec.n_clients)
            ]
        )
        TaxProfile.objects.bulk_create(
            [
                TaxProfile(
                    id=demo_id(spec.seed, f"profile:{index}"),
                    tenant=tenant,
                    client=client,
                    regime=TaxRegime.MEI,
                    valid_from=period,
                    provenance="staff_entry",
                )
                for index, client in enumerate(clients)
            ]
        )
        Engagement.objects.bulk_create(
            [
                Engagement(
                    id=demo_id(spec.seed, f"engagement:{index}"),
                    tenant=tenant,
                    client=client,
                    service_kind=ServiceKind.MEI_MENSAL,
                    starts_on=period,
                    responsible=owner,
                    provenance="staff_entry",
                )
                for index, client in enumerate(clients)
            ]
        )
        Obligation.objects.bulk_create(
            [
                Obligation(
                    id=demo_id(spec.seed, f"obligation:{index}"),
                    tenant=tenant,
                    client=client,
                    obligation_type_id="DAS",
                    competence_month=period,
                    nominal_due_date=spec.now.date() + timedelta(days=2),
                    resolved_due_date=spec.now.date() + timedelta(days=2),
                    status=ObligationStatus.DUE,
                )
                for index, client in enumerate(clients)
            ]
        )
    return clients


def add_portfolio_variations(spec: ScaleSpec, *, mixed: bool) -> None:
    """Add deterministic long names, expired metadata and a mixed-regime portfolio."""
    clients = list(ClientCompany.objects.order_by("cnpj"))
    profiles = {row.client_id: row for row in TaxProfile.objects.all()}
    engagements = {row.client_id: row for row in Engagement.objects.all()}
    regimes = (TaxRegime.MEI, TaxRegime.SIMPLES, TaxRegime.PRESUMIDO, TaxRegime.REAL)
    for index, client in enumerate(clients):
        client.legal_name = (
            f"Empresa sintética {spec.seed}-{index:04d} de comércio, tecnologia, "
            "serviços especializados e manutenção industrial com nome extenso Ltda."
        )
        client.has_digital_certificate = index % 4 == 0
        client.certificate_expires_on = (
            spec.now.date() - timedelta(days=30) if index % 4 == 0 else None
        )
        profile = profiles[client.pk]
        profile.valid_from = HISTORY_START
        profile.provenance = "seed_axorim_demo"
        profile.regime = regimes[index % len(regimes)] if mixed else TaxRegime.MEI
        client.is_mei = profile.regime == TaxRegime.MEI
        engagement = engagements[client.pk]
        engagement.starts_on = HISTORY_START
        engagement.provenance = "seed_axorim_demo"
        engagement.service_kind = (
            ServiceKind.MEI_MENSAL if client.is_mei else ServiceKind.DOCUMENTOS_MENSAL
        )
    ClientCompany.objects.bulk_update(
        clients,
        ["legal_name", "has_digital_certificate", "certificate_expires_on", "is_mei"],
    )
    TaxProfile.objects.bulk_update(
        profiles.values(), ["valid_from", "provenance", "regime"]
    )
    Engagement.objects.bulk_update(
        engagements.values(), ["starts_on", "provenance", "service_kind"]
    )
    # A non-MEI synthetic client must not carry a contradictory MEI DAS calendar.
    Obligation.objects.filter(client__is_mei=False).delete()
