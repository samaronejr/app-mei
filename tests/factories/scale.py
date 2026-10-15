"""Deterministic active portfolios for the briefing budget and later scale work."""

from apps.core.demo_data import ScaleSpec, build_portfolio
from tests.briefing.fixtures import NOW
from tests.ui.factories import Firm, make_firm


def build_firm(n_clients: int, seed: int) -> Firm:
    """Build active, scoped clients with real profiles, engagements and due facts."""
    spec = ScaleSpec(n_clients, seed, NOW)
    firm = make_firm(f"briefing-scale-{seed}")
    firm.tenant.features = ["cycles_r1", "briefing_r1"]
    firm.tenant.save(update_fields=["features"])
    firm.clients.extend(build_portfolio(firm.tenant, firm.owner, spec))
    return firm
