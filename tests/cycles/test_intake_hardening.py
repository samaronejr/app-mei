"""T12 intake hardening (S8.3, S15 B/C): quarantine, sniffing, scan port, safe parsers.

Real portal uploads through the middleware (role switch, RLS) and the real services;
the only double is `FakeScanner` at the scanner port, which is the port's purpose.
Time is injected (`now=`), never frozen or slept.
"""

import builtins
import socket
import threading
import urllib.request
from collections.abc import Iterator
from datetime import timedelta
from http import HTTPStatus
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.db import transaction
from django.utils import timezone
from pypdf import PdfWriter
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import counted_links, quarantine_key
from apps.cycles.models import (
    CycleEvent,
    DocumentIntake,
    EvidenceLink,
    IntakeState,
)
from apps.cycles.models import Exception as CycleException
from apps.cycles.parsing import (
    UnsafeDocument,
    parse_intake_xml,
    parse_xml,
    pdf_text,
)
from apps.cycles.scanning import (
    EICAR,
    ClamdScanner,
    FakeScanner,
    IntegrityMismatch,
    ScannerUnavailable,
    ScanOutcome,
    claim_scan,
    due_intakes,
    promote,
    record_verdict,
    run_scan,
    scan_intake,
)
from apps.cycles.sniffing import JPEG, PDF, PNG, XML, sniff
from apps.cycles.tasks import scan_quarantine, scan_tenant_quarantine
from apps.obligations.models import Document
from tests.portal.request_fixtures import PortalSetup, portal_setup, upload

# Only the tests that touch the database carry this: the scanner port refuses to run
# inside a transaction, so the socket and parser tests must not be wrapped in one.
DB = pytest.mark.django_db(transaction=True)

PDF_BYTES = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"
NFE_XML = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe><infNFe Id="x">'
    b"<emit><CNPJ>11222333000181</CNPJ></emit></infNFe></NFe></nfeProc>"
)
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 64
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64
REJECTED_COPY = "Este arquivo não foi aceito"


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    settings.MEDIA_ROOT = tmp_path
    settings.CLAMAV_ENABLED = False
    return portal_setup("t12-intake")


def _post(setup: PortalSetup, name: str, payload: bytes, content_type: str) -> Any:  # noqa: ANN401
    return setup.portal().post(
        f"/solicitacoes/{setup.request.pk}/enviar",
        {"file": upload(name, payload, content_type)},
    )


def _intake(setup: PortalSetup) -> DocumentIntake:
    with tenant_context(setup.graph.tenant.pk):
        return DocumentIntake.objects.select_related("document").get()


def _scan(setup: PortalSetup, intake: DocumentIntake, **kwargs: Any) -> ScanOutcome:  # noqa: ANN401
    kwargs.setdefault("now", timezone.now())
    return scan_intake(setup.graph.tenant.pk, intake.pk, **kwargs)


# --- quarantine and EICAR ---------------------------------------------------------


@DB
def test_upload_lands_in_quarantine_and_nowhere_else(setup: PortalSetup) -> None:
    response = _post(setup, "nota.pdf", PDF_BYTES, "text/html")
    assert response.status_code == HTTPStatus.FOUND
    intake = _intake(setup)
    document = intake.document
    assert intake.state == IntakeState.QUARANTINED
    assert intake.sniffed_type == PDF
    # The declared text/html is ignored; the stored type is the sniffed one.
    assert document.content_type == PDF
    assert intake.quarantine_key == quarantine_key(
        setup.graph.tenant.pk, setup.graph.client.pk, document.storage_key
    )
    assert default_storage.exists(intake.quarantine_key)
    assert not default_storage.exists(document.storage_key)
    # Served to nobody in the portal -- not even its uploader -- before a verdict.
    portal = setup.portal()
    assert portal.get(f"/documentos/{document.storage_key}").status_code == 404
    assert "nota.pdf" not in portal.get("/documentos/").content.decode()
    staff = setup.firm().get(f"/documentos/{document.pk}/baixar/")
    assert staff.status_code == HTTPStatus.CONFLICT
    assert PDF_BYTES not in staff.content


@DB
def test_eicar_is_rejected_as_malware_not_linked_not_downloadable(
    setup: PortalSetup,
) -> None:
    payload = PDF_BYTES + EICAR
    assert _post(setup, "virus.pdf", payload, "application/pdf").status_code == 302
    intake = _intake(setup)
    scanner = FakeScanner()

    outcome = _scan(setup, intake, scanner=scanner)

    assert outcome is ScanOutcome.INFECTED
    assert scanner.scanned == [len(payload)]
    intake = _intake(setup)
    assert intake.state == IntakeState.REJECTED_MALWARE
    assert intake.verdict == "infectado"
    assert intake.reason == "malware:Eicar-Test-Signature"
    assert intake.scan_engine == "fake"
    assert intake.scanned_at is not None
    # Retained, never deleted, never promoted to the final key.
    assert default_storage.exists(intake.quarantine_key)
    assert not default_storage.exists(intake.document.storage_key)
    with tenant_context(setup.graph.tenant.pk):
        assert EvidenceLink.objects.count() == 1
        assert counted_links().count() == 0
        assert CycleEvent.objects.filter(type="intake.scanned").count() == 1
    portal = setup.portal()
    key = intake.document.storage_key
    assert portal.get(f"/documentos/{key}").status_code == HTTPStatus.NOT_FOUND
    page = portal.get(f"/solicitacoes/{setup.request.pk}/").content.decode()
    assert "virus.pdf" not in page
    assert 'data-status="recebido"' not in page
    staff = setup.firm().get(f"/documentos/{intake.document_id}/baixar/")
    assert staff.status_code == HTTPStatus.FORBIDDEN
    assert EICAR not in staff.content
    forced = setup.firm().post(
        f"/documentos/{intake.document_id}/baixar/", {"confirmar": "1"}
    )
    assert forced.status_code == HTTPStatus.FORBIDDEN
    assert EICAR not in forced.content
    cycle_page = setup.firm().get(f"/ciclos/{setup.cycle.pk}/").content.decode()
    assert "recusado: ameaça detectada" in cycle_page
    assert "Recebido do cliente" not in cycle_page
    # The same bytes again: refused, and still no counted link.
    again = _post(setup, "virus-2.pdf", payload, "application/pdf")
    assert again.status_code == HTTPStatus.BAD_REQUEST
    assert REJECTED_COPY in again.content.decode()
    with tenant_context(setup.graph.tenant.pk):
        assert EvidenceLink.objects.count() == 1
        assert Document.objects.count() == 1


@DB
def test_clean_verdict_moves_bytes_out_of_quarantine(setup: PortalSetup) -> None:
    _post(setup, "nota.pdf", PDF_BYTES, "application/pdf")
    intake = _intake(setup)

    assert _scan(setup, intake, scanner=FakeScanner()) is ScanOutcome.VERIFIED

    intake = _intake(setup)
    assert intake.state == IntakeState.VERIFIED
    assert intake.verdict == "limpo"
    assert not default_storage.exists(intake.quarantine_key)
    with default_storage.open(intake.document.storage_key) as handle:
        assert handle.read() == PDF_BYTES
    own = setup.portal().get(f"/documentos/{intake.document.storage_key}")
    assert own.status_code == HTTPStatus.OK
    assert own.content == PDF_BYTES
    assert setup.firm().get(f"/documentos/{intake.document_id}/baixar/").content == (
        PDF_BYTES
    )
    with tenant_context(setup.graph.tenant.pk):
        assert counted_links().count() == 1
    # A second scan of the same intake is a no-op: nothing pending.
    assert _scan(setup, intake, scanner=FakeScanner()) is ScanOutcome.SKIPPED


# --- type sniffing ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (PDF_BYTES, PDF),
        (NFE_XML, XML),
        (b"\xef\xbb\xbf  \n" + NFE_XML, XML),
        (PNG_BYTES, PNG),
        (JPEG_BYTES, JPEG),
    ],
)
def test_sniff_accepts_the_allowlist(payload: bytes, expected: str) -> None:
    assert sniff(payload) == expected


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"<!DOCTYPE html><html><body>x</body></html>", id="html"),
        pytest.param(b"<html><script>alert(1)</script></html>", id="html-bare"),
        pytest.param(
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>',
            id="svg",
        ),
        pytest.param(
            b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>',
            id="svg-with-declaration",
        ),
        pytest.param(b"PK\x03\x04\x14\x00\x00\x00" + b"\x00" * 40, id="zip"),
        pytest.param(
            b"PK\x03\x04\x14\x00\x06\x00\x08\x00\x00\x00!\x00"
            + b"\x00" * 16
            + b"[Content_Types].xml"
            + b"\x00" * 40
            + b"word/document.xml",
            id="docx",
        ),
        pytest.param(b"MZ\x90\x00\x03" + b"\x00" * 60, id="exe"),
        pytest.param(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic", id="heic"),
        pytest.param(b"%PDF-1.4\n<html><script>alert(1)</script>", id="pdf+html"),
        pytest.param(
            b"%PDF-1.4\n" + b"%" * 8000 + b"<script>alert(1)</script>",
            id="pdf+late-script",
        ),
        pytest.param(PNG_BYTES + b"<svg onload=alert(1)>", id="png+svg"),
        pytest.param(b"GIF89a" + b"\x00" * 32, id="gif"),
        pytest.param(b"  junk %PDF-1.4\n", id="pdf-offset"),
        pytest.param(b"plain text that is not xml", id="text"),
    ],
)
def test_sniff_refuses_everything_else(payload: bytes) -> None:
    assert sniff(payload) is None


@DB
def test_polyglot_upload_is_rejected_retained_and_not_linked(
    setup: PortalSetup,
) -> None:
    polyglot = b"%PDF-1.4\n<html><body><script>alert(document.domain)</script>"
    response = _post(setup, "nota.pdf", polyglot, "application/pdf")
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert REJECTED_COPY in response.content.decode()
    intake = _intake(setup)
    assert intake.state == IntakeState.REJECTED_TYPE
    assert intake.sniffed_type == ""
    assert intake.document.content_type == "application/octet-stream"
    assert default_storage.exists(intake.quarantine_key)
    with tenant_context(setup.graph.tenant.pk):
        assert EvidenceLink.objects.count() == 0
        assert CycleEvent.objects.filter(type="evidence.submitted").count() == 0
    # Rejected bytes are never handed to a scanner either.
    assert _scan(setup, intake, scanner=FakeScanner()) is ScanOutcome.SKIPPED
    assert setup.firm().get(
        f"/documentos/{intake.document_id}/baixar/"
    ).status_code == (HTTPStatus.FORBIDDEN)


# --- scanner down, deadline and the disabled switch -------------------------------


@DB
def test_scanner_down_stays_quarantined_retries_then_opens_exception(
    setup: PortalSetup,
) -> None:
    _post(setup, "nota.pdf", PDF_BYTES, "application/pdf")
    intake = _intake(setup)
    start = intake.created_at
    down = FakeScanner(down=True)

    assert _scan(setup, intake, scanner=down, now=start) is ScanOutcome.RETRY
    intake = _intake(setup)
    assert intake.state == IntakeState.QUARANTINED
    assert intake.scan_attempts == 1
    assert intake.next_scan_at == start + timedelta(minutes=1)
    tenant = setup.graph.tenant.pk
    assert due_intakes(tenant, start) == []
    assert due_intakes(tenant, start + timedelta(minutes=1)) == [intake.pk]

    near = start + timedelta(hours=23, minutes=59)
    assert _scan(setup, intake, scanner=down, now=near) is ScanOutcome.RETRY
    assert _intake(setup).state == IntakeState.QUARANTINED
    with tenant_context(tenant):
        assert not CycleException.objects.exists()

    late = start + timedelta(hours=24)
    assert _scan(setup, intake, scanner=down, now=late) is ScanOutcome.UNAVAILABLE
    intake = _intake(setup)
    assert intake.state == IntakeState.SCAN_UNAVAILABLE
    assert intake.reason == "scanner_indisponivel_24h"
    assert intake.scan_attempts == 3
    with tenant_context(tenant):
        problem = CycleException.objects.get()
    assert problem.reason_code == "verificacao_indisponivel"
    assert problem.subject_ref == intake.pk
    assert problem.cycle_id == setup.cycle.pk
    assert problem.state == "aberta"
    # Still quarantined bytes; never re-served by the portal.
    assert default_storage.exists(intake.quarantine_key)
    key = intake.document.storage_key
    assert setup.portal().get(f"/documentos/{key}").status_code == 404


@DB
def test_disabled_scanner_means_unavailable_staff_warning_only(
    setup: PortalSetup,
) -> None:
    _post(setup, "nota.pdf", PDF_BYTES, "application/pdf")
    tenant = setup.graph.tenant.pk

    assert scan_quarantine.apply().get() == 1

    intake = _intake(setup)
    assert intake.state == IntakeState.SCAN_UNAVAILABLE
    assert intake.reason == "clamav_desativado"
    with tenant_context(tenant):
        assert not CycleException.objects.exists()
    firm = setup.firm()
    url = f"/documentos/{intake.document_id}/baixar/"
    warning = firm.get(url)
    assert warning.status_code == HTTPStatus.OK
    assert PDF_BYTES not in warning.content
    body = warning.content.decode()
    assert "não passou por verificação antivírus" in body
    assert 'name="confirmar" value="1"' in body
    assert firm.post(url, {}).status_code == HTTPStatus.OK  # no confirmation: warning
    confirmed = firm.post(url, {"confirmar": "1"})
    assert confirmed.status_code == HTTPStatus.OK
    assert confirmed.content == PDF_BYTES
    assert confirmed["Content-Security-Policy"] == "sandbox"
    with tenant_context(tenant):
        assert (
            CycleEvent.objects.filter(type="document.unscanned_downloaded").count() == 1
        )
    portal = setup.portal()
    assert portal.get(f"/documentos/{intake.document.storage_key}").status_code == 404
    assert "nota.pdf" not in portal.get("/documentos/").content.decode()


@DB
def test_unassigned_staff_accountant_is_denied_and_writes_nothing(
    setup: PortalSetup,
) -> None:
    _post(setup, "nota.pdf", PDF_BYTES, "application/pdf")
    _scan(setup, _intake(setup), scanner=None)
    intake = _intake(setup)
    staff = setup.firm(setup.graph.staff)
    url = f"/documentos/{intake.document_id}/baixar/"
    assert staff.get(url).status_code == HTTPStatus.NOT_FOUND
    assert staff.post(url, {"confirmar": "1"}).status_code == HTTPStatus.NOT_FOUND
    with tenant_context(setup.graph.tenant.pk):
        assert not CycleEvent.objects.filter(
            type="document.unscanned_downloaded"
        ).exists()


@DB
def test_sweep_skips_tenants_without_the_flag(setup: PortalSetup) -> None:
    _post(setup, "nota.pdf", PDF_BYTES, "application/pdf")
    tenant = setup.graph.tenant
    tenant.features = []
    tenant.save(update_fields=["features"])
    assert scan_quarantine.apply().get() == 0
    assert _intake(setup).state == IntakeState.QUARANTINED


def test_the_scan_sweep_is_on_the_beat_schedule(settings: SettingsWrapper) -> None:
    entry = settings.CELERY_BEAT_SCHEDULE["scan-quarantine"]
    assert entry["task"] == "apps.cycles.tasks.scan_quarantine"
    assert entry["schedule"] == 60.0


@DB
def test_stale_verdict_cannot_overwrite_a_newer_one(setup: PortalSetup) -> None:
    payload = PDF_BYTES + EICAR
    _post(setup, "nota.pdf", payload, "application/pdf")
    intake = _intake(setup)
    tenant = setup.graph.tenant.pk
    first = claim_scan(tenant, intake.pk)
    second = claim_scan(tenant, intake.pk)
    assert first is not None
    assert second is not None
    infected = FakeScanner().scan(BytesIO(payload))
    clean = FakeScanner().scan(BytesIO(PDF_BYTES))

    assert record_verdict(first, infected, now=timezone.now()) is ScanOutcome.INFECTED
    assert record_verdict(second, clean, now=timezone.now()) is ScanOutcome.STALE

    intake = _intake(setup)
    assert intake.state == IntakeState.REJECTED_MALWARE
    assert intake.version == first.version + 1
    with tenant_context(tenant):
        assert CycleEvent.objects.filter(type="intake.scanned").count() == 1


@DB
def test_a_crash_between_copy_and_verdict_resumes_idempotently(
    setup: PortalSetup,
) -> None:
    _post(setup, "nota.pdf", PDF_BYTES, "application/pdf")
    intake = _intake(setup)
    tenant = setup.graph.tenant.pk
    ticket = claim_scan(tenant, intake.pk)
    assert ticket is not None
    verdict = run_scan(ticket, FakeScanner(), default_storage)
    promote(ticket, default_storage)
    # The worker dies here: bytes copied to the final key, no verdict recorded.
    assert default_storage.exists(intake.document.storage_key)
    assert _intake(setup).state == IntakeState.QUARANTINED
    assert not verdict.infected

    assert _scan(setup, intake, scanner=FakeScanner()) is ScanOutcome.VERIFIED

    intake = _intake(setup)
    assert intake.version == ticket.version + 1
    assert not default_storage.exists(intake.quarantine_key)
    with default_storage.open(intake.document.storage_key) as handle:
        assert handle.read() == PDF_BYTES
    # Repeated sweeps after the verdict change nothing.
    assert scan_tenant_quarantine(str(tenant)) == {}


@DB
def test_tampered_bytes_are_never_promoted_and_the_sweep_does_not_report_success(
    setup: PortalSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    _post(setup, "nota.pdf", PDF_BYTES, "application/pdf")
    intake = _intake(setup)
    # An object already at the final key that is not the uploaded bytes.
    default_storage.save(intake.document.storage_key, BytesIO(b"%PDF-1.4 other"))
    with pytest.raises(IntegrityMismatch):
        _scan(setup, intake, scanner=FakeScanner())
    assert _intake(setup).state == IntakeState.QUARANTINED
    # The sweep, with the scanner port pointed at the fake, fails loudly.
    monkeypatch.setattr("apps.cycles.tasks.scanner_from_settings", FakeScanner)
    with pytest.raises(RuntimeError, match="1 intake scan"):
        scan_tenant_quarantine(str(setup.graph.tenant.pk))
    assert _intake(setup).state == IntakeState.QUARANTINED


@DB
def test_scanner_io_refuses_to_run_inside_a_transaction(setup: PortalSetup) -> None:
    _post(setup, "nota.pdf", PDF_BYTES, "application/pdf")
    intake = _intake(setup)
    with transaction.atomic(), pytest.raises(RuntimeError, match="External I/O"):
        _scan(setup, intake, scanner=FakeScanner())
    with transaction.atomic(), pytest.raises(RuntimeError, match="External I/O"):
        FakeScanner().scan(BytesIO(PDF_BYTES))
    assert _intake(setup).state == IntakeState.QUARANTINED


# --- reconcile --------------------------------------------------------------------


@DB
def test_reconcile_checks_quarantine_keys_and_reports_leftovers(
    setup: PortalSetup,
) -> None:
    _post(setup, "nota.pdf", PDF_BYTES, "application/pdf")
    intake = _intake(setup)
    stdout = StringIO()
    call_command("reconcile_documents", stdout=stdout)
    report = stdout.getvalue()
    assert "missing=0" in report
    assert "hash_mismatches=0" in report
    assert "orphan_candidates=0" in report

    leftover = default_storage.save(
        quarantine_key(setup.graph.tenant.pk, setup.graph.client.pk, "stray"),
        BytesIO(b"stray"),
    )
    default_storage.delete(intake.quarantine_key)
    stdout = StringIO()
    call_command("reconcile_documents", stdout=stdout)
    report = stdout.getvalue()
    assert f"CRITICAL row-without-object tenant={setup.graph.tenant.pk} " in report
    assert f"storage_key={intake.quarantine_key}" in report
    assert f"ORPHAN storage_key={leftover} " in report


# --- clamd INSTREAM ---------------------------------------------------------------


class _FakeClamd:
    """A one-shot-per-connection clamd on 127.0.0.1 that records INSTREAM frames."""

    def __init__(self, reply: bytes, *, silent: bool = False) -> None:
        self.reply = reply
        self.silent = silent
        self.frames: list[int] = []
        self.received = b""
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _read(self, conn: socket.socket, size: int) -> bytes:
        data = b""
        while len(data) < size:
            part = conn.recv(size - len(data))
            if not part:
                break
            data += part
        return data

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                command = b""
                while not command.endswith(b"\0"):
                    part = conn.recv(1)
                    if not part:
                        break
                    command += part
                if command == b"zVERSION\0":
                    conn.sendall(b"ClamAV 1.4.1/27421/Mon Sep 28 08:00:00 2026\0")
                    continue
                while True:
                    size = int.from_bytes(self._read(conn, 4), "big")
                    if size == 0:
                        break
                    self.frames.append(size)
                    self.received += self._read(conn, size)
                if self.silent:
                    self.release.wait(timeout=5)
                    continue
                conn.sendall(self.reply)

    def close(self) -> None:
        self.release.set()
        self.server.close()
        self.thread.join(timeout=5)


@pytest.fixture
def clamd() -> Iterator[type[_FakeClamd]]:
    servers: list[_FakeClamd] = []

    class Factory(_FakeClamd):
        def __init__(self, reply: bytes, *, silent: bool = False) -> None:
            super().__init__(reply, silent=silent)
            servers.append(self)

    yield Factory
    for server in servers:
        server.close()


def test_clamd_instream_chunks_and_parses_found(clamd: type[_FakeClamd]) -> None:
    server = clamd(b"stream: Eicar-Test-Signature FOUND\0")
    scanner = ClamdScanner("127.0.0.1", server.port, timeout=2, chunk_bytes=16)
    payload = b"0123456789" * 5 + EICAR

    verdict = scanner.scan(BytesIO(payload))

    assert verdict.infected is True
    assert verdict.signature == "Eicar-Test-Signature"
    assert verdict.engine == "ClamAV 1.4.1"
    assert verdict.signature_version.startswith("27421/")
    assert server.received == payload
    assert server.frames[:-1] == [16] * (len(payload) // 16)
    assert max(server.frames) == 16


def test_clamd_ok_is_clean(clamd: type[_FakeClamd]) -> None:
    server = clamd(b"stream: OK\0")
    verdict = ClamdScanner("127.0.0.1", server.port, timeout=2).scan(BytesIO(PDF_BYTES))
    assert verdict.infected is False


@pytest.mark.parametrize(
    "reply", [b"INSTREAM size limit exceeded. ERROR\0", b"stream: garbage\0", b""]
)
def test_clamd_without_a_verdict_is_unavailable(
    clamd: type[_FakeClamd], reply: bytes
) -> None:
    server = clamd(reply)
    with pytest.raises(ScannerUnavailable):
        ClamdScanner("127.0.0.1", server.port, timeout=2).scan(BytesIO(PDF_BYTES))


def test_clamd_timeout_and_refused_connection_are_unavailable(
    clamd: type[_FakeClamd],
) -> None:
    silent = clamd(b"", silent=True)
    with pytest.raises(ScannerUnavailable, match="TimeoutError"):
        ClamdScanner("127.0.0.1", silent.port, timeout=0.2).scan(BytesIO(PDF_BYTES))
    closed = socket.create_server(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    with pytest.raises(ScannerUnavailable):
        ClamdScanner("127.0.0.1", port, timeout=1).scan(BytesIO(PDF_BYTES))


def test_clamd_refuses_streams_over_the_size_limit(clamd: type[_FakeClamd]) -> None:
    server = clamd(b"stream: OK\0")
    scanner = ClamdScanner("127.0.0.1", server.port, timeout=2, max_bytes=32)
    with pytest.raises(ScannerUnavailable, match="size limit"):
        scanner.scan(BytesIO(b"x" * 33))


# --- safe parsing -----------------------------------------------------------------


@pytest.fixture
def no_egress(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fail loudly, and record, any network or file access while parsing."""
    touched: list[str] = []

    def refuse(kind: str) -> Any:  # noqa: ANN401
        def guard(*args: object, **kwargs: object) -> None:
            touched.append(f"{kind}:{args[:2]!r}")
            msg = f"parser attempted {kind}"
            raise AssertionError(msg)

        return guard

    monkeypatch.setattr(socket.socket, "connect", refuse("connect"))
    monkeypatch.setattr(socket, "create_connection", refuse("create_connection"))
    monkeypatch.setattr(urllib.request, "urlopen", refuse("urlopen"))
    monkeypatch.setattr(builtins, "open", refuse("open"))
    return touched


def _xxe_payloads(canary: Path) -> dict[str, bytes]:
    return {
        "xxe-file": (
            f'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file://{canary}">]>'
            "<r>&x;</r>"
        ).encode(),
        "xxe-http": (
            b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM '
            b'"http://127.0.0.1:9/xxe">]><r>&x;</r>'
        ),
        "external-dtd": b'<?xml version="1.0"?><!DOCTYPE r SYSTEM "http://127.0.0.1:9/e.dtd"><r/>',
        "parameter-entity": (
            b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY % p SYSTEM '
            b'"http://127.0.0.1:9/p.dtd"> %p;]><r/>'
        ),
        "billion-laughs": (
            b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
            b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
            b'<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">'
            b"]><lolz>&lol3;</lolz>"
        ),
    }


@pytest.mark.parametrize(
    "case",
    ["xxe-file", "xxe-http", "external-dtd", "parameter-entity", "billion-laughs"],
)
def test_xxe_and_billion_laughs_are_refused_without_io(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canary = tmp_path / "canary.txt"
    canary.write_text("CANARY-SECRET")
    payload = _xxe_payloads(canary)[case]
    touched: list[str] = []

    def refuse(kind: str) -> Any:  # noqa: ANN401
        def guard(*args: object, **kwargs: object) -> None:
            touched.append(kind)
            msg = f"parser attempted {kind}"
            raise AssertionError(msg)

        return guard

    monkeypatch.setattr(socket.socket, "connect", refuse("connect"))
    monkeypatch.setattr(socket, "create_connection", refuse("create_connection"))
    monkeypatch.setattr(urllib.request, "urlopen", refuse("urlopen"))
    monkeypatch.setattr(builtins, "open", refuse("open"))

    with pytest.raises(UnsafeDocument) as refused:
        parse_xml(payload)

    assert refused.value.code == "dtd_forbidden"
    assert touched == []


def test_plain_nfe_xml_parses_without_io(no_egress: list[str]) -> None:
    root = parse_xml(NFE_XML)
    assert root.tag.endswith("nfeProc")
    assert no_egress == []


def test_malformed_xml_is_a_typed_refusal() -> None:
    with pytest.raises(UnsafeDocument) as refused:
        parse_xml(b"<r><unclosed></r>")
    assert refused.value.code == "malformed"


def _pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def test_pdf_page_limit_and_deadline() -> None:
    assert pdf_text(_pdf(50)) == "\n" * 49
    with pytest.raises(UnsafeDocument) as too_long:
        pdf_text(_pdf(51))
    assert too_long.value.code == "too_many_pages"
    ticks = iter([0.0, 0.0, 11.0])
    with pytest.raises(UnsafeDocument) as slow:
        pdf_text(_pdf(3), clock=lambda: next(ticks))
    assert slow.value.code == "timeout"
    with pytest.raises(UnsafeDocument) as broken:
        pdf_text(b"%PDF-1.4 not really a pdf")
    assert broken.value.code == "malformed"


@DB
def test_nothing_is_parsed_before_a_clean_verdict(setup: PortalSetup) -> None:
    _post(setup, "nota.xml", NFE_XML, "text/xml")
    intake = _intake(setup)
    assert intake.sniffed_type == XML
    with pytest.raises(UnsafeDocument) as refused:
        parse_intake_xml(intake, NFE_XML)
    assert refused.value.code == "not_verified"
    _scan(setup, intake, scanner=FakeScanner())
    assert parse_intake_xml(_intake(setup), NFE_XML).tag.endswith("nfeProc")
