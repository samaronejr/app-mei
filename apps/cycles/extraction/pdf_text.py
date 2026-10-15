"""CNPJs and the competence month from a PDF's embedded text.

The text comes from `apps.cycles.parsing.pdf_text` (50 pages, 10 s between pages,
inside the extraction task's Celery soft limit). A PDF without embedded text -- a
scan -- is "não verificável automaticamente": there is no OCR in R1.
"""

import re
from typing import Final

from apps.cycles.evidence import FieldOrigin
from apps.cycles.extraction import NO_TEXT, UNVERIFIABLE, Extraction, FieldValue
from apps.cycles.extraction.cnpj import find

MAX_CNPJS: Final = 5
MONTHS: Final[dict[str, int]] = {
    "janeiro": 1,
    "fevereiro": 2,
    "março": 3,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}
# Accepted spellings of the competence month: a numeric month and year separated by
# a slash, dash or dot, or a pt-BR month name followed by "/" or "de" and the year.
COMPETENCE: Final = re.compile(
    r"compet[eê]ncia\s*[:\-]?\s*(?:de\s+)?"
    r"(?:(?P<month>0[1-9]|1[0-2])\s*[/\-.]\s*(?P<year>20\d{2})"
    r"|(?P<name>[a-zç]+)\s*(?:/|de)\s*(?P<named_year>20\d{2}))",
    re.IGNORECASE,
)


def _competence(text: str) -> FieldValue | None:
    for match in COMPETENCE.finditer(text):
        if match.group("month"):
            month, year = int(match.group("month")), match.group("year")
        else:
            number = MONTHS.get(match.group("name").lower())
            if number is None:
                continue
            month, year = number, match.group("named_year")
        return FieldValue(
            "competencia",
            f"{year}-{month:02d}",
            FieldOrigin.PDF_TEXT,
            f"{match.start()}-{match.end()}",
        )
    return None


def extract(text: str) -> Extraction:
    """Every valid CNPJ (up to five, first occurrence) and the competence month."""
    if not text.strip():
        return Extraction(kind="pdf", note=NO_TEXT)
    fields: list[FieldValue] = []
    parties: list[str] = []
    for value, start, end in find(text):
        if value in parties:
            continue
        parties.append(value)
        fields.append(
            FieldValue(
                f"cnpj_{len(parties)}", value, FieldOrigin.PDF_TEXT, f"{start}-{end}"
            )
        )
        if len(parties) == MAX_CNPJS:
            break
    competence = _competence(text)
    if competence is not None:
        fields.append(competence)
    return Extraction(
        kind="pdf",
        fields=tuple(fields),
        parties=tuple(parties),
        note="" if parties else UNVERIFIABLE,
    )
