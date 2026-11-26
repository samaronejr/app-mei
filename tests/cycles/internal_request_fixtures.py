"""Real private obligations and staff proof for internal request integration tests."""

from uuid import UUID

from django.apps.registry import Apps

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


def legacy_guide_requests(
    apps: Apps, setup: PortalSetup
) -> tuple[UUID, dict[UUID, str | None]]:
    """Seed the pre-purpose schema with a title match, a recorded guide and decoys."""
    legacy = apps.get_model("cycles", "EvidenceRequest")._base_manager
    graph = setup.graph
    fields = {
        "tenant_id": graph.tenant.pk,
        "client_id": graph.client.pk,
        "cycle_id": setup.cycle.pk,
        "title": "Guia DAS oficial",
        "created_by_id": graph.actor.pk,
        "required": False,
    }
    namesake = legacy.create(**fields, state="interno")
    prior_ordinary_cutover(setup, namesake.pk)
    guide = legacy.create(**fields, state="interno", version=3)
    other = legacy.create(**fields, state="solicitado")
    duplicate = legacy.create(**fields, state="interno")
    peer = {
        **fields,
        "client_id": setup.other_client.pk,
        "cycle_id": setup.other_request.cycle_id,
    }
    peer_namesake = legacy.create(**peer, state="interno")
    recovered = legacy.create(**{**peer, "title": "Renamed"}, state="interno")
    apps.get_model("cycles", "CycleEvent")._base_manager.create(
        tenant_id=graph.tenant.pk,
        client_id=setup.other_client.pk,
        cycle_id=setup.other_request.cycle_id,
        actor_id=graph.actor.pk,
        type="request.created",
        aggregate_type="EvidenceRequest",
        aggregate_id=recovered.pk,
        aggregate_version=1,
        payload={"origin": "pgmei"},
    )
    purposes = {row.pk: None for row in (namesake, peer_namesake, other, duplicate)}
    return guide.pk, {
        **purposes,
        guide.pk: "guia_das_oficial",
        recovered.pk: "guia_das_oficial",
    }
