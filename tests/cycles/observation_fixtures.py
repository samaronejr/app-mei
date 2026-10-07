"""Real receipt and cycle surfaces shared by the T17 repair regressions."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import EvidenceLink, EvidenceRequest
from apps.cycles.models import CycleEvent, ServiceCycle
from apps.cycles.services.requests import RequestFields, create_request, send_requests
from apps.obligations.coverage import CoverageItem, coverage_for
from apps.obligations.generator import generate_das_calendar
from apps.obligations.models import Obligation, SourceObservation
from tests.cycles.cycle_fixtures import PERIOD
from tests.portal.request_fixtures import PortalSetup, portal_setup

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse


@pytest.fixture
def receipt_case(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    """Use the real portal fixture with its default request explicitly bound."""
    settings.MEDIA_ROOT = tmp_path
    base = portal_setup("t17-repair")
    with tenant_context(base.graph.tenant.pk):
        generate_das_calendar(base.graph.client, starting_from=PERIOD)
        subject = Obligation.objects.get(
            client=base.graph.client, competence_month=PERIOD, obligation_type_id="DAS"
        )
        request = create_request(
            base.graph.actor,
            base.cycle.pk,
            RequestFields(
                "Comprovante do DAS", kind="comprovante", obligation_id=subject.pk
            ),
            expected_version=base.cycle.version,
        )
        base.cycle.refresh_from_db()
        send_requests(
            base.graph.actor, base.cycle.pk, expected_version=base.cycle.version
        )
        base.cycle.refresh_from_db()
        request.refresh_from_db()
    return replace(base, request=request)


def payment_subject(setup: PortalSetup) -> Obligation:
    subject_id = setup.request.obligation_id
    assert subject_id is not None
    with tenant_context(setup.graph.tenant.pk):
        return Obligation.objects.get(pk=subject_id)


def payment_claim(
    subject: Obligation,
    *,
    source: str = "staff_recorded",
    verification: str = "unknown",
    observed_at: datetime | None = None,
    outcome: str = "recorded",
) -> SourceObservation:
    """Seed reserved reader inputs, never a mock of a producer under test."""
    with tenant_context(subject.tenant_id):
        return SourceObservation.objects.create(
            tenant_id=subject.tenant_id,
            client_id=subject.client_id,
            subject_ref=subject,
            kind="payment",
            period=subject.competence_month,
            source_kind=source,
            verification_status=verification,
            observed_at=observed_at or timezone.now(),
            outcome=outcome,
            source_ref="AXR1-017-proof",
        )


def coverage_item(setup: PortalSetup, *, now: datetime | None = None) -> CoverageItem:
    with tenant_context(setup.graph.tenant.pk):
        return coverage_for(
            setup.graph.actor,
            setup.graph.client,
            PERIOD,
            engagement_id=setup.cycle.engagement_id,
            now=now,
        )[0]


def staff_post(setup: PortalSetup, **overrides: str) -> "_MonkeyPatchedWSGIResponse":
    """Post the revision originally rendered, not a refreshed command revision."""
    fields = {
        "action": "observation",
        "op": "record",
        "subject_id": str(setup.request.obligation_id),
        "kind": "payment",
        "expected_version": str(setup.cycle.version),
        "source_ref": "AXR1-017-proof",
    }
    fields.update(overrides)
    return setup.firm().post(f"/ciclos/{setup.cycle.pk}/", fields)


def review_post(
    setup: PortalSetup, link: EvidenceLink, **overrides: str
) -> "_MonkeyPatchedWSGIResponse":
    with tenant_context(setup.graph.tenant.pk):
        version = EvidenceRequest.objects.get(pk=link.request_id).version
    fields = {
        "decision": "aceito",
        "client_reason": "",
        "expected_version": str(version),
        "payment_relevant": "1",
    }
    fields.update(overrides)
    return setup.firm().post(f"/documentos/{link.pk}/revisar/", fields)


def staff_counts(setup: PortalSetup) -> tuple[int, int, int]:
    with tenant_context(setup.graph.tenant.pk):
        return (
            SourceObservation.objects.count(),
            CycleEvent.objects.filter(cycle=setup.cycle).count(),
            ServiceCycle.objects.get(pk=setup.cycle.pk).version,
        )
