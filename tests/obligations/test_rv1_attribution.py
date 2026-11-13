"""Exercise attribution after an existing fiscal identity has a newer revision."""

import pytest

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake, EvidenceLink, IntakeState
from apps.cycles.records import Exception as CycleException
from tests.obligations.fiscal_fixtures import COUNTERPART, nfe
from tests.obligations.test_fiscal_documents import ingest, setup
from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["setup"]


@pytest.mark.parametrize("incoming_version", [11, 12, 13])
def test_wrong_recipient_always_opens_the_attribution_exception(
    setup: PortalSetup,
    incoming_version: int,
) -> None:
    ingest(setup, version=12, payload=nfe())
    ingest(setup, version=incoming_version, payload=nfe(recipient=COUNTERPART))
    with tenant_context(setup.graph.tenant.pk):
        assert DocumentIntake.objects.filter(state=IntakeState.DIVERGENT).exists()
        reasons = set(CycleException.objects.values_list("reason_code", flat=True))
        assert "documento_fiscal_divergente" in reasons, (
            f"Mismatched recipient at provider version {incoming_version} was "
            f"classified divergent without the required attribution exception; "
            f"actual reason codes: {sorted(reasons)}"
        )
        assert not EvidenceLink.objects.exists()
