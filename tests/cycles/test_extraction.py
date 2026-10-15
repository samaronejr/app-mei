"""T13 deterministic extraction (S7.5, M5): NF-e, national NFS-e and PDF text.

The pure extractors run over the synthetic fixtures in tests/fixtures/documents/;
the end-to-end cases go through the real upload, the scanner port (`FakeScanner`) and
the extraction service. The soft-time-limit path is driven by a patched reader whose
page raises what Celery raises in a worker -- no sleep, no timer.
"""

from pathlib import Path
from typing import Any

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from django.core.files.storage import default_storage
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles import parsing
from apps.cycles.evidence import (
    CLEAN_STATES,
    DocumentIntake,
    ExtractedField,
    FieldOrigin,
    IntakeState,
)
from apps.cycles.extraction import (
    EXTRACTOR_VERSION,
    INTERRUPTED,
    LAYOUT_UNKNOWN,
    NO_TEXT,
    UNVERIFIABLE,
    Extraction,
    nfe_xml,
    nfse_xml,
    pdf_text,
)
from apps.cycles.extraction.service import (
    ExtractionOutcome,
    claim_extraction,
    extract_intake,
    record_extraction,
)
from apps.cycles.models import CycleEvent
from apps.cycles.parsing import PDF_TIMEOUT_SECONDS, parse_xml
from apps.cycles.tasks import (
    EXTRACTION_HARD_LIMIT_SECONDS,
    EXTRACTION_SOFT_LIMIT_SECONDS,
    extract_document,
    extract_documents,
)
from apps.obligations.models import Document
from tests.cycles.document_fixtures import (
    CLIENT_CNPJ,
    COUNTERPART_CNPJ,
    extract,
    fixture,
    reviewed,
    scan_clean,
    submit,
)
from tests.portal.request_fixtures import PortalSetup, portal_setup

DB = pytest.mark.django_db(transaction=True)
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 64


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    settings.MEDIA_ROOT = tmp_path
    return portal_setup("t13-extraction")


def _values(extraction: Extraction) -> dict[str, str]:
    return {field.name: field.value for field in extraction.fields}


# --- pure extractors over the synthetic fixtures ------------------------------------


def test_nfe_reads_parties_number_date_and_total_with_their_paths() -> None:
    extraction = nfe_xml.extract(parse_xml(fixture("nfe_emitente_cliente.xml")))

    assert extraction is not None
    assert _values(extraction) == {
        "emitente_cnpj": CLIENT_CNPJ,
        "destinatario_cnpj": COUNTERPART_CNPJ,
        "numero": "123",
        "data_emissao": "2026-09-15",
        "valor_total": "1500.00",
    }
    assert extraction.parties == (CLIENT_CNPJ, COUNTERPART_CNPJ)
    assert {field.origin for field in extraction.fields} == {FieldOrigin.XML}
    spans = {field.name: field.source_span for field in extraction.fields}
    assert spans["emitente_cnpj"] == "emit/CNPJ"


def test_nfse_national_layout_reads_known_version() -> None:
    root = parse_xml(fixture("nfse_nacional.xml"))

    assert nfe_xml.extract(root) is None
    extraction = nfse_xml.extract(root)

    assert extraction is not None
    assert extraction.note == ""
    assert _values(extraction) == {
        "prestador_cnpj": CLIENT_CNPJ,
        "tomador_cnpj": COUNTERPART_CNPJ,
        "numero": "42",
        "competencia": "2026-09",
        "data_emissao": "2026-09-20",
        "valor_servico": "800.00",
    }


def test_nfse_unknown_version_yields_empty_fields_and_layout_note() -> None:
    extraction = nfse_xml.extract(parse_xml(fixture("nfse_versao_desconhecida.xml")))

    assert extraction is not None
    assert extraction.fields == ()
    assert extraction.parties == ()
    assert extraction.note == LAYOUT_UNKNOWN


def test_pdf_text_finds_valid_cnpjs_masked_and_alphanumeric_and_competence() -> None:
    text = parsing.pdf_text(fixture("recibo_cliente.pdf"))

    extraction = pdf_text.extract(text)

    assert extraction.parties == (CLIENT_CNPJ, COUNTERPART_CNPJ)
    assert _values(extraction) == {
        "cnpj_1": CLIENT_CNPJ,
        "cnpj_2": COUNTERPART_CNPJ,
        "competencia": "2026-09",
    }
    assert {field.origin for field in extraction.fields} == {FieldOrigin.PDF_TEXT}


def test_pdf_text_ignores_bad_check_digits_and_runs_inside_longer_numbers() -> None:
    text = (
        "CNPJ 12.ABC.345/01DE-34 (dígito errado) e chave "
        "11222333000181999999999999999999999999999999 sem separação. "
        "Competência de setembro de 2026"
    )

    extraction = pdf_text.extract(text)

    assert extraction.parties == ()
    assert extraction.note == UNVERIFIABLE
    assert _values(extraction) == {"competencia": "2026-09"}


def test_pdf_without_text_is_not_verifiable() -> None:
    extraction = pdf_text.extract(
        parsing.pdf_text(fixture("digitalizado_sem_texto.pdf"))
    )

    assert extraction.fields == ()
    assert extraction.note == NO_TEXT


# --- through upload, scan and the extraction service --------------------------------


@DB
def test_verified_nfe_is_classified_with_provenance_and_the_original_untouched(
    setup: PortalSetup,
) -> None:
    payload = fixture("nfe_emitente_cliente.xml")
    intake, _link = reviewed(setup, "nfe_emitente_cliente.xml")

    assert intake.state == IntakeState.CLASSIFIED
    assert intake.reason == ""
    with tenant_context(setup.graph.tenant.pk):
        fields = list(ExtractedField.objects.filter(document_id=intake.document_id))
        document = Document.objects.get(pk=intake.document_id)
        assert CycleEvent.objects.filter(type="document.extracted").count() == 1
    assert {field.name for field in fields} == {
        "emitente_cnpj",
        "destinatario_cnpj",
        "numero",
        "data_emissao",
        "valor_total",
    }
    assert {field.extractor_version for field in fields} == {EXTRACTOR_VERSION}
    assert {field.origin for field in fields} == {FieldOrigin.XML}
    # S7.5: extraction never writes into the document or its bytes.
    with default_storage.open(document.storage_key, "rb") as handle:
        assert handle.read() == payload
    assert document.content_type == "application/xml"


@DB
def test_nothing_is_parsed_before_a_clean_verdict(setup: PortalSetup) -> None:
    intake = submit(setup, "nfe_emitente_cliente.xml")
    assert intake.state == IntakeState.QUARANTINED

    outcome = extract(setup, intake)

    assert outcome is ExtractionOutcome.SKIPPED
    with tenant_context(setup.graph.tenant.pk):
        assert not ExtractedField.objects.exists()
        assert DocumentIntake.objects.get(pk=intake.pk).state == IntakeState.QUARANTINED
    with pytest.raises(parsing.UnsafeDocument) as refused:
        parsing.parse_intake_xml(intake, fixture("nfe_emitente_cliente.xml"))
    assert refused.value.code == parsing.NOT_VERIFIED


@DB
@pytest.mark.parametrize(
    ("name", "payload", "note"),
    [
        ("nfse_versao_desconhecida.xml", None, LAYOUT_UNKNOWN),
        ("xml_desconhecido.xml", None, LAYOUT_UNKNOWN),
        ("digitalizado_sem_texto.pdf", None, NO_TEXT),
        ("foto.png", PNG_BYTES, UNVERIFIABLE),
        ("quebrado.xml", b"<?xml version='1.0'?><nota><aberta>", "arquivo_ilegivel"),
    ],
)
def test_unreadable_or_unknown_files_are_classified_empty_never_crash(
    setup: PortalSetup, name: str, payload: bytes | None, note: str
) -> None:
    intake, _link = reviewed(setup, name, payload)

    assert intake.state == IntakeState.CLASSIFIED
    assert intake.reason.startswith(note)
    with tenant_context(setup.graph.tenant.pk):
        assert not ExtractedField.objects.exists()


@DB
def test_stale_ticket_writes_nothing(setup: PortalSetup) -> None:
    intake = submit(setup, "nfe_emitente_cliente.xml")
    scan_clean(setup, intake)
    stale = claim_extraction(setup.graph.tenant.pk, intake.pk)
    assert stale is not None
    assert extract(setup, intake) is ExtractionOutcome.CLASSIFIED

    outcome = record_extraction(
        stale,
        nfe_xml.extract(parse_xml(fixture("nfe_outra_empresa.xml")))
        or Extraction(kind="nfe"),
        now=timezone.now(),
    )

    assert outcome is ExtractionOutcome.STALE
    with tenant_context(setup.graph.tenant.pk):
        assert ExtractedField.objects.count() == 5
        assert DocumentIntake.objects.get(pk=intake.pk).state == IntakeState.CLASSIFIED


# --- the Celery soft time limit bounds one slow page --------------------------------


class _SlowPage:
    """A page whose text extraction is interrupted the way a worker interrupts it."""

    def extract_text(self) -> str:
        raise SoftTimeLimitExceeded


class _SlowReader:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:  # noqa: ANN401
        self.pages = [_SlowPage()]


def test_extraction_task_carries_a_soft_limit_above_the_page_deadline() -> None:
    assert extract_document.soft_time_limit == EXTRACTION_SOFT_LIMIT_SECONDS
    assert extract_document.time_limit == EXTRACTION_HARD_LIMIT_SECONDS
    assert PDF_TIMEOUT_SECONDS < EXTRACTION_SOFT_LIMIT_SECONDS
    assert EXTRACTION_SOFT_LIMIT_SECONDS < EXTRACTION_HARD_LIMIT_SECONDS


@DB
def test_soft_limit_inside_one_page_is_recorded_as_interrupted(
    setup: PortalSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    intake = submit(setup, "recibo_outra_empresa.pdf")
    scan_clean(setup, intake)
    monkeypatch.setattr(parsing, "PdfReader", _SlowReader)

    result = extract_document(str(setup.graph.tenant.pk), str(intake.pk))

    assert result == ExtractionOutcome.CLASSIFIED.value
    with tenant_context(setup.graph.tenant.pk):
        fresh = DocumentIntake.objects.get(pk=intake.pk)
        assert not ExtractedField.objects.exists()
    # Interrupted is "não verificável", never divergent and never a crash.
    assert fresh.state == IntakeState.CLASSIFIED
    assert fresh.reason == INTERRUPTED


# --- the beat queues flagged firms only ---------------------------------------------


@DB
def test_beat_queues_verified_intakes_of_flagged_firms_only(
    setup: PortalSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    intake = submit(setup, "nfe_emitente_cliente.xml")
    scan_clean(setup, intake)
    queued: list[tuple[str, str]] = []
    monkeypatch.setattr(
        extract_document, "delay", lambda *args: queued.append(tuple(args))
    )

    assert extract_documents() == 1
    assert queued == [(str(setup.graph.tenant.pk), str(intake.pk))]

    tenant = setup.graph.tenant
    tenant.features = []
    tenant.save(update_fields=["features"])
    queued.clear()
    assert extract_documents() == 0
    assert queued == []


@DB
def test_extraction_moves_only_to_clean_states(setup: PortalSetup) -> None:
    intake, _link = reviewed(setup, "recibo_cliente.pdf")
    assert intake.state in CLEAN_STATES
    # A classified intake is never extracted twice: the suggestions are written once.
    assert (
        extract_intake(setup.graph.tenant.pk, intake.pk, now=timezone.now())
        is ExtractionOutcome.SKIPPED
    )
    with tenant_context(setup.graph.tenant.pk):
        assert ExtractedField.objects.count() == 3
