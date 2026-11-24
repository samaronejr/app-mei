"""Real private obligations and staff proof for internal request integration tests."""

from uuid import UUID

from apps.audit.models import Event as AuditEvent
from apps.cycles.evidence import (
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    IntakeState,
    LinkOrigin,
    ScanVerdict,
)
from apps.cycles.intake import staff_upload
from apps.obligations.models import Document, Obligation
from tests.cycles.document_fixtures import fixture
from tests.portal.request_fixtures import PortalSetup, upload


def filing_subject(setup: PortalSetup, **overrides: object) -> Obligation:
    """Private municipal subject in this cycle unless a test overrides its scope."""
    period = setup.cycle.period_start
    fields = {
        "tenant": setup.graph.tenant,
        "client": setup.graph.client,
        "obligation_type_id": "DAS",
        "competence_month": period,
        "nominal_due_date": period,
        "resolved_due_date": period,
        "portal_visible": False,
        **overrides,
    }
    return Obligation.objects.create(**fields)


def attach_filing(
    setup: PortalSetup, request: EvidenceRequest
) -> tuple[EvidenceLink, DocumentIntake]:
    """Attach a reviewable staff document to the selected request."""
    graph = setup.graph
    document = Document.objects.create(
        tenant=graph.tenant,
        client=graph.client,
        sha256="a" * 64,
        original_filename="filing.pdf",
        content_type="application/pdf",
        byte_size=50,
        uploaded_by=graph.actor,
        client_upload=False,
        portal_visible=False,
    )
    link = EvidenceLink.objects.create(
        tenant=graph.tenant,
        client=graph.client,
        request=request,
        document=document,
        origin=LinkOrigin.STAFF,
        submitted_by=graph.actor,
    )
    intake = DocumentIntake.objects.create(
        tenant=graph.tenant,
        client=graph.client,
        document=document,
        state=IntakeState.CLASSIFIED,
        verdict=ScanVerdict.CLEAN,
    )
    return link, intake


def upload_for_internal_review(
    setup: PortalSetup, request: EvidenceRequest
) -> DocumentIntake:
    """Attach actual synthetic XML uploaded by the staff member, not the system."""
    graph = setup.graph
    fixture_name = "nfe_outra_empresa.xml"
    document, _ = staff_upload(
        graph.actor,
        upload(fixture_name, fixture(fixture_name), "application/xml"),
        tenant=graph.tenant,
        client=graph.client,
    )
    intake = DocumentIntake.objects.get(document=document)
    EvidenceLink.objects.create(
        tenant=graph.tenant,
        client=graph.client,
        document=document,
        request=request,
        origin=LinkOrigin.STAFF,
        submitted_by=graph.actor,
    )
    return intake


def prior_ordinary_cutover(setup: PortalSetup, request_id: UUID) -> None:
    """The factual cutover report for an ordinary row hidden due to a history gap."""
    graph = setup.graph
    AuditEvent.objects.create(
        tenant_id=graph.tenant.pk,
        actor_id=None,
        action="cycles.request_visibility_repaired",
        object_type="EvidenceRequest",
        object_id=str(request_id),
        metadata={
            "client_id": str(graph.client.pk),
            "cycle_id": str(setup.cycle.pk),
            "previous_state": "solicitado",
            "previous_version": 3,
            "reason": "revision_gap_or_duplicate",
        },
    )
