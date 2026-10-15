"""The only parsers allowed near client bytes, and only after a clean verdict (S8.3).

XML goes through `defusedxml.ElementTree` with DTDs forbidden outright: no internal
entity (billion laughs), no external entity or DTD (XXE), and so no network or file
access while parsing. PDF text goes through `pypdf`, refusing more than 50 pages and
stopping at a 10 s deadline between pages; the extraction task that calls it (T13)
runs under a Celery soft time limit of the same budget, which also bounds one slow
page.

`parse_intake_*` refuse any intake the scanner has not declared clean (`verificado`,
or a later clean state, T13): nothing is parsed before the scanner has said clean.
"""

import time
from collections.abc import Callable
from io import BytesIO
from typing import Final
from xml.etree.ElementTree import Element, ParseError

import defusedxml
from defusedxml import ElementTree as SafeElementTree
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from apps.cycles.evidence import CLEAN_STATES, DocumentIntake

PDF_MAX_PAGES: Final = 50
PDF_TIMEOUT_SECONDS: Final = 10.0

NOT_VERIFIED: Final = "not_verified"
DTD_FORBIDDEN: Final = "dtd_forbidden"
ENTITIES_FORBIDDEN: Final = "entities_forbidden"
EXTERNAL_FORBIDDEN: Final = "external_forbidden"
MALFORMED: Final = "malformed"
TOO_MANY_PAGES: Final = "too_many_pages"
TIMEOUT: Final = "timeout"


class UnsafeDocument(Exception):  # noqa: N818 - domain vocabulary
    """The document was refused by a parser guard; `code` says which."""

    def __init__(self, code: str) -> None:
        """Keep a machine-readable refusal code."""
        super().__init__(code)
        self.code = code


def require_verified(intake: DocumentIntake) -> None:
    """Refuse to parse anything the scanner has not declared clean."""
    if intake.state not in CLEAN_STATES:
        raise UnsafeDocument(NOT_VERIFIED)


def parse_xml(payload: bytes) -> Element:
    """Parse XML with DTDs, entities and external references forbidden."""
    try:
        return SafeElementTree.fromstring(
            payload, forbid_dtd=True, forbid_entities=True, forbid_external=True
        )
    except defusedxml.DTDForbidden as error:
        raise UnsafeDocument(DTD_FORBIDDEN) from error
    except defusedxml.EntitiesForbidden as error:
        raise UnsafeDocument(ENTITIES_FORBIDDEN) from error
    except defusedxml.ExternalReferenceForbidden as error:
        raise UnsafeDocument(EXTERNAL_FORBIDDEN) from error
    except ParseError as error:
        raise UnsafeDocument(MALFORMED) from error


def pdf_text(
    payload: bytes,
    *,
    max_pages: int = PDF_MAX_PAGES,
    timeout: float = PDF_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Embedded text of a PDF of at most `max_pages`, within `timeout` seconds."""
    deadline = clock() + timeout
    try:
        reader = PdfReader(BytesIO(payload))
        if len(reader.pages) > max_pages:
            raise UnsafeDocument(TOO_MANY_PAGES)
        parts: list[str] = []
        for page in reader.pages:
            if clock() > deadline:
                raise UnsafeDocument(TIMEOUT)
            parts.append(page.extract_text() or "")
    except (PyPdfError, ValueError, KeyError, TypeError) as error:
        raise UnsafeDocument(MALFORMED) from error
    return "\n".join(parts)


def parse_intake_xml(intake: DocumentIntake, payload: bytes) -> Element:
    """Parse a verified intake's XML."""
    require_verified(intake)
    return parse_xml(payload)


def parse_intake_pdf(intake: DocumentIntake, payload: bytes) -> str:
    """Extract a verified intake's PDF text."""
    require_verified(intake)
    return pdf_text(payload)
