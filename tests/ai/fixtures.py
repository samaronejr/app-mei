"""A single-company synthetic PDF through the real intake/extraction services."""

from apps.automation.dataset import minimal_pdf
from apps.cycles.evidence import DocumentIntake
from tests.cycles.document_fixtures import reviewed
from tests.portal.request_fixtures import PortalSetup


def single_company(setup: PortalSetup, number: int = 1) -> DocumentIntake:
    payload = minimal_pdf(
        f"Recibo {number}. CNPJ: 11222333000181. Competencia: 09/2026"
    )
    intake, _ = reviewed(setup, "recibo_cliente.pdf", payload)
    return intake
