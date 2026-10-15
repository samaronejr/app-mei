"""Upload, scan and extract a synthetic fixture through the real T11/T12/T13 path.

The portal upload goes through the middleware (role switch, RLS); the scan uses the
`FakeScanner` port double; extraction is the real service. Only `now` is injected.
"""

from pathlib import Path
from typing import Final

from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake, EvidenceLink
from apps.cycles.extraction.service import ExtractionOutcome, extract_intake
from apps.cycles.scanning import FakeScanner, ScanOutcome, scan_intake
from tests.portal.request_fixtures import PortalSetup, upload

FIXTURES: Final = Path(__file__).resolve().parent.parent / "fixtures" / "documents"
CLIENT_CNPJ: Final = "11222333000181"
OTHER_CLIENT_CNPJ: Final = "11444777000161"
COUNTERPART_CNPJ: Final = "12ABC34501DE35"
STRANGER_CNPJ: Final = "98ZYX76501AB50"
CONTENT_TYPES: Final = {".xml": "application/xml", ".pdf": "application/pdf"}


def fixture(name: str) -> bytes:
    """Read one synthetic fixture."""
    return (FIXTURES / name).read_bytes()


def submit(
    setup: PortalSetup, name: str, payload: bytes | None = None
) -> DocumentIntake:
    """Upload a fixture (or `payload` under `name`) against the setup's request."""
    body = fixture(name) if payload is None else payload
    content_type = CONTENT_TYPES.get(Path(name).suffix, "application/octet-stream")
    response = setup.portal().post(
        f"/solicitacoes/{setup.request.pk}/enviar",
        {"file": upload(name, body, content_type)},
    )
    assert response.status_code == 302, response.content
    with tenant_context(setup.graph.tenant.pk):
        return (
            DocumentIntake.objects.select_related("document")
            .filter(document__original_filename=name)
            .get()
        )


def scan_clean(setup: PortalSetup, intake: DocumentIntake) -> None:
    """Give the intake a clean verdict through the scanner port."""
    outcome = scan_intake(
        setup.graph.tenant.pk, intake.pk, scanner=FakeScanner(), now=timezone.now()
    )
    assert outcome is ScanOutcome.VERIFIED


def extract(setup: PortalSetup, intake: DocumentIntake) -> ExtractionOutcome:
    """Run the extraction service for one intake."""
    return extract_intake(setup.graph.tenant.pk, intake.pk, now=timezone.now())


def reviewed(
    setup: PortalSetup, name: str, payload: bytes | None = None
) -> tuple[DocumentIntake, EvidenceLink]:
    """Upload, scan clean and extract; return the fresh intake and its link."""
    intake = submit(setup, name, payload)
    scan_clean(setup, intake)
    extract(setup, intake)
    with tenant_context(setup.graph.tenant.pk):
        fresh = DocumentIntake.objects.select_related("document").get(pk=intake.pk)
        link = EvidenceLink.objects.get(document_id=intake.document_id)
    return fresh, link
