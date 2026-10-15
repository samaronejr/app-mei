"""Deterministic field extraction from clean client documents (T13, M5, S7.5).

Three extractors, no AI and no network:

- `nfe_xml` reads the NF-e parties, number, date and total from the NF-e namespace;
- `nfse_xml` reads the national NFS-e layout, and answers "layout não reconhecido"
  with empty fields for any version or shape it does not know -- never a crash;
- `pdf_text` finds CNPJs (alphanumeric, check digits validated) and a competence
  month in a PDF's embedded text.

`extract` only ever parses through `apps.cycles.parsing` (defusedxml / pypdf with page
and time limits), which refuses any intake the scanner has not declared clean. Images
are never read: without OCR in R1 they are "não verificável automaticamente".

Every value is a *suggestion* with its origin. Nothing here writes to the document.
"""

from dataclasses import dataclass
from typing import Final

from apps.cycles.evidence import DocumentIntake
from apps.cycles.parsing import UnsafeDocument, parse_intake_pdf, parse_intake_xml
from apps.cycles.sniffing import JPEG, PDF, PNG, XML

EXTRACTOR_VERSION: Final = "r1.1"

# Machine codes stored in `DocumentIntake.reason`; the review page renders pt-BR.
LAYOUT_UNKNOWN: Final = "layout_nao_reconhecido"
UNVERIFIABLE: Final = "nao_verificavel"
NO_TEXT: Final = "pdf_sem_texto"
INTERRUPTED: Final = "extracao_interrompida"
UNREADABLE: Final = "arquivo_ilegivel"
DIVERGENT: Final = "cnpj_divergente"

# pt-BR labels for every field an extractor may suggest, in display order. A
# reviewer's correction is accepted only for a name listed here.
FIELD_LABELS: Final[dict[str, str]] = {
    "emitente_cnpj": "CNPJ do emitente",
    "destinatario_cnpj": "CNPJ do destinatário",
    "prestador_cnpj": "CNPJ do prestador",
    "tomador_cnpj": "CNPJ do tomador",
    "cnpj_1": "CNPJ encontrado (1)",
    "cnpj_2": "CNPJ encontrado (2)",
    "cnpj_3": "CNPJ encontrado (3)",
    "cnpj_4": "CNPJ encontrado (4)",
    "cnpj_5": "CNPJ encontrado (5)",
    "numero": "Número",
    "competencia": "Competência",
    "data_emissao": "Data de emissão",
    "valor_total": "Valor total",
    "valor_servico": "Valor do serviço",
}
# The rows the review table always offers per kind, so a reviewer can type a value
# the extractor could not read (an image, an unknown layout).
KIND_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "nfe": (
        "emitente_cnpj",
        "destinatario_cnpj",
        "numero",
        "data_emissao",
        "valor_total",
    ),
    "nfse": (
        "prestador_cnpj",
        "tomador_cnpj",
        "numero",
        "competencia",
        "data_emissao",
        "valor_servico",
    ),
    "pdf": ("cnpj_1", "competencia"),
    "image": ("cnpj_1", "competencia"),
    "xml": ("cnpj_1", "competencia"),
}
# The XML layouts an extractor recognises, in the order they are tried.
XML_LAYOUTS: Final = ("nfe", "nfse")


@dataclass(frozen=True, slots=True)
class FieldValue:
    """One suggested value, where it came from and where in the source it was."""

    name: str
    value: str
    origin: str
    source_span: str = ""


@dataclass(frozen=True, slots=True)
class Extraction:
    """What one extractor read. `parties`: valid CNPJs the document names."""

    kind: str
    fields: tuple[FieldValue, ...] = ()
    parties: tuple[str, ...] = ()
    note: str = ""


def is_cnpj_field(name: str) -> bool:
    """Whether a field holds a CNPJ (validated on correction)."""
    return name.endswith("_cnpj") or name.startswith("cnpj_")


def kind_for(sniffed_type: str) -> str:
    """Return the extractor family a sniffed type belongs to."""
    if sniffed_type == PDF:
        return "pdf"
    if sniffed_type in {PNG, JPEG}:
        return "image"
    return "xml"


def extract(intake: DocumentIntake, payload: bytes) -> Extraction:
    """Run the extractor for the intake's sniffed type over its clean bytes.

    A parser refusal (malformed XML, a DTD, too many pages, the page deadline) is an
    answer, not a crash: empty fields and a note the reviewer reads.
    """
    # Imported here: each extractor imports `Extraction` from this module.
    from apps.cycles.extraction import (  # noqa: PLC0415
        nfe_xml,
        nfse_xml,
        pdf_text,
    )

    sniffed = intake.sniffed_type
    if sniffed in {PNG, JPEG}:
        return Extraction(kind="image", note=UNVERIFIABLE)
    try:
        if sniffed == PDF:
            return pdf_text.extract(parse_intake_pdf(intake, payload))
        if sniffed == XML:
            root = parse_intake_xml(intake, payload)
            return (
                nfe_xml.extract(root)
                or nfse_xml.extract(root)
                or Extraction(kind="xml", note=LAYOUT_UNKNOWN)
            )
    except UnsafeDocument as refusal:
        return Extraction(
            kind=kind_for(sniffed), note=f"{UNREADABLE}:{refusal.code}"[:64]
        )
    return Extraction(kind=kind_for(sniffed), note=UNVERIFIABLE)


__all__ = [
    "DIVERGENT",
    "EXTRACTOR_VERSION",
    "FIELD_LABELS",
    "INTERRUPTED",
    "KIND_FIELDS",
    "LAYOUT_UNKNOWN",
    "NO_TEXT",
    "UNREADABLE",
    "UNVERIFIABLE",
    "XML_LAYOUTS",
    "Extraction",
    "FieldValue",
    "extract",
    "is_cnpj_field",
    "kind_for",
]
