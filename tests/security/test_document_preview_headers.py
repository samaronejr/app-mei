"""Served client files: sandboxed, non-sniffable, safely named (S8.3 "Previews", T12).

Every path that hands stored bytes back to a browser -- the portal vault download and
the firm-side download, including the unscanned-file download behind the warning --
goes through `apps.cycles.serving.document_response`.
"""

from http import HTTPStatus
from pathlib import Path

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.models import DocumentIntake
from apps.cycles.scanning import FakeScanner, scan_intake
from apps.cycles.serving import document_response, safe_filename
from apps.obligations.models import Document
from tests.portal.request_fixtures import PortalSetup, portal_setup, upload

# Only the tests that touch the database carry this: the scanner port refuses to run
# inside a transaction, so the socket and parser tests must not be wrapped in one.
DB = pytest.mark.django_db(transaction=True)

PDF = b"%PDF-1.4\nsynthetic\n%%EOF\n"
HOSTILE_NAME = 'nota"\r\nSet-Cookie: pwned=1;\t\u202e.pdf'


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    settings.MEDIA_ROOT = tmp_path
    return portal_setup("t12-headers")


def _assert_safe(response: object, *, attachment: bool = True) -> str:
    headers = response.headers  # type: ignore[attr-defined]
    assert headers["Content-Security-Policy"] == "sandbox"
    assert headers["X-Content-Type-Options"] == "nosniff"
    disposition = headers["Content-Disposition"]
    assert disposition.startswith("attachment" if attachment else "inline")
    assert "\r" not in disposition
    assert "\n" not in disposition
    assert "Set-Cookie" not in headers
    return str(disposition)


def _verified(setup: PortalSetup, name: str) -> DocumentIntake:
    response = setup.portal().post(
        f"/solicitacoes/{setup.request.pk}/enviar",
        {"file": upload(name, PDF, "text/html")},
    )
    assert response.status_code == HTTPStatus.FOUND
    with tenant_context(setup.graph.tenant.pk):
        intake = DocumentIntake.objects.get()
    scan_intake(
        setup.graph.tenant.pk, intake.pk, scanner=FakeScanner(), now=timezone.now()
    )
    with tenant_context(setup.graph.tenant.pk):
        return DocumentIntake.objects.select_related("document").get()


@DB
def test_portal_download_is_sandboxed_nosniff_and_rfc5987_named(
    setup: PortalSetup,
) -> None:
    intake = _verified(setup, "Nota fiscal de São João.pdf")
    response = setup.portal().get(f"/documentos/{intake.document.storage_key}")
    assert response.status_code == HTTPStatus.OK
    disposition = _assert_safe(response)
    assert "filename*=utf-8''Nota%20fiscal%20de%20S%C3%A3o%20Jo%C3%A3o.pdf" in (
        disposition
    )
    # The declared text/html was never trusted: the sniffed type is served.
    assert response["Content-Type"] == "application/pdf"


@DB
def test_hostile_filename_cannot_inject_a_header(setup: PortalSetup) -> None:
    intake = _verified(setup, "nota.pdf")
    # A multipart header cannot carry CR/LF, so the stored name is set directly.
    with tenant_context(setup.graph.tenant.pk):
        Document.objects.filter(pk=intake.document_id).update(
            original_filename=HOSTILE_NAME
        )
    for response in (
        setup.portal().get(f"/documentos/{intake.document.storage_key}"),
        setup.firm().get(f"/documentos/{intake.document_id}/baixar/"),
    ):
        assert response.status_code == HTTPStatus.OK
        disposition = _assert_safe(response)
        assert "\t" not in disposition
        assert "\u202e" not in disposition
        assert "pwned" not in response.cookies


@DB
def test_staff_download_carries_the_same_headers(setup: PortalSetup) -> None:
    intake = _verified(setup, "extrato.pdf")
    response = setup.firm().get(f"/documentos/{intake.document_id}/baixar/")
    assert response.status_code == HTTPStatus.OK
    assert response.content == PDF
    assert _assert_safe(response) == 'attachment; filename="extrato.pdf"'


@DB
def test_legacy_vault_file_with_a_declared_html_type_is_octet_stream(
    setup: PortalSetup,
) -> None:
    graph = setup.graph
    with tenant_context(graph.tenant.pk):
        document = Document(
            tenant=graph.tenant,
            client=graph.client,
            sha256="0" * 64,
            original_filename="page.html",
            content_type="text/html",
            byte_size=len(b"<script>alert(1)</script>"),
            uploaded_by=setup.portal_user,
            # T13: only a firm-published row is served without an intake; a client
            # upload without one fails closed (tests/portal/test_vault_quarantine.py).
            client_upload=False,
        )
        default_storage.save(
            document.storage_key, ContentFile(b"<script>alert(1)</script>")
        )
        document.save(force_insert=True)
    response = setup.portal().get(f"/documentos/{document.storage_key}")
    assert response.status_code == HTTPStatus.OK
    _assert_safe(response)
    assert response["Content-Type"] == "application/octet-stream"
    # Never scanned: the firm gets the warning first, the bytes only after its POST.
    url = f"/documentos/{document.pk}/baixar/"
    warning = setup.firm().get(url)
    assert warning.status_code == HTTPStatus.OK
    assert b"<script>alert(1)</script>" not in warning.content
    assert "antes da verificação existir" in warning.content.decode()
    confirmed = setup.firm().post(url, {"confirmar": "1"})
    assert confirmed.status_code == HTTPStatus.OK
    _assert_safe(confirmed)
    assert confirmed["Content-Type"] == "application/octet-stream"


def test_inline_preview_keeps_the_sandbox() -> None:
    response = document_response(
        PDF, content_type="application/pdf", filename="nota.pdf", inline=True
    )
    assert _assert_safe(response, attachment=False) == 'inline; filename="nota.pdf"'
    assert response["Content-Type"] == "application/pdf"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("nota.pdf", "nota.pdf"),
        ("../../etc/passwd", "etc passwd"),
        ("a\r\nb.pdf", "ab.pdf"),
        ("\u202egpj.exe", "gpj.exe"),
        ("", "documento"),
        (" . ", "documento"),
    ],
)
def test_safe_filename(raw: str, expected: str) -> None:
    assert safe_filename(raw) == expected
