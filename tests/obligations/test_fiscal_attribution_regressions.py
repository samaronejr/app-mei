"""Attribution problems survive every version outcome without duplicate exceptions."""

import pytest

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake, EvidenceLink, IntakeState
from apps.cycles.records import Exception as CycleException
from apps.obligations.models import FiscalDocument, FiscalRevision
from tests.obligations.fiscal_fixtures import COUNTERPART, nfe
from tests.obligations.test_fiscal_documents import document, ingest, setup
from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["setup"]


def test_stale_wrong_recipient_opens_one_exception_without_moving_the_head(
    setup: PortalSetup,
) -> None:
    ingest(setup, version=12, payload=nfe())
    before = document(setup)
    for _ in range(2):
        ingest(setup, version=11, payload=nfe(recipient=COUNTERPART))
        with tenant_context(setup.graph.tenant.pk):
            problems = CycleException.objects.filter(
                reason_code="documento_fiscal_divergente",
                subject_ref=before.pk,
            )
            assert problems.count() == 1
            current = FiscalDocument.objects.get(pk=before.pk)
            assert current.current_revision_id == before.current_revision_id
            assert current.version == before.version
            assert current.role_verification == "confirmed"
            assert current.fiscal_state == "autorizada"
            assert FiscalRevision.objects.count() == 2
            assert DocumentIntake.objects.filter(state=IntakeState.DIVERGENT).exists()
            assert not EvidenceLink.objects.exists()


def test_same_version_wrong_recipient_opens_one_exception_alongside_the_incident(
    setup: PortalSetup,
) -> None:
    ingest(setup, version=12, payload=nfe())
    before = document(setup)
    for _ in range(2):
        ingest(setup, version=12, payload=nfe(recipient=COUNTERPART))
        with tenant_context(setup.graph.tenant.pk):
            problems = CycleException.objects.filter(
                reason_code="documento_fiscal_divergente",
                subject_ref=before.pk,
            )
            assert problems.count() == 1
            assert (
                CycleException.objects.filter(
                    reason_code="incidente_consistencia_fiscal",
                    subject_ref=before.pk,
                ).count()
                == 1
            )
            current = FiscalDocument.objects.get(pk=before.pk)
            assert current.current_revision_id == before.current_revision_id
            assert current.role_verification == "confirmed"
            assert current.fiscal_state == "incerta"
            assert FiscalRevision.objects.count() == 1
            assert DocumentIntake.objects.filter(state=IntakeState.DIVERGENT).exists()
            assert not EvidenceLink.objects.exists()
