"""Unsigned XML generation and safe inspection; signing never happens in Axorim."""

import base64
import hashlib
import re
from dataclasses import dataclass
from datetime import date
from html import escape
from typing import TYPE_CHECKING, Final
from xml.etree.ElementTree import Element

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from django.core.files.storage import default_storage

from apps.integrations.serpro.codec import Parties
from apps.integrations.transport_types import JSONValue

if TYPE_CHECKING:
    from apps.obligations.models import Document

MAX_XML: Final = 5 * 1024 * 1024
SIGNATURE_NS: Final = "http://www.w3.org/2000/09/xmldsig#"
CHILDREN: Final = (
    "sistema",
    "termo",
    "avisoLegal",
    "finalidade",
    "dataAssinatura",
    "vigencia",
    "destinatario",
    "assinadoPor",
)


class TermoInvalidoError(ValueError):
    """A symbolic boundary refusal that never includes the uploaded XML."""


@dataclass(frozen=True, slots=True)
class LegalNotices:
    """The firm supplies the three current official legal texts before signing."""

    authorization: str
    privacy: str
    purpose: str


@dataclass(frozen=True, slots=True)
class TermoDraft:
    """Public identities, dates and notices; no certificate or signing credentials."""

    contratante_name: str
    firm_name: str
    signed_on: date
    valid_until: date
    notices: LegalNotices


def legal_notices(value: JSONValue) -> LegalNotices:
    """Parse only the three operator-supplied legal texts, not credential fields."""
    if not isinstance(value, dict) or set(value) != {
        "authorization",
        "privacy",
        "purpose",
    }:
        raise TermoInvalidoError
    authorization, privacy, purpose = (
        value["authorization"],
        value["privacy"],
        value["purpose"],
    )
    if (
        not isinstance(authorization, str)
        or not isinstance(privacy, str)
        or not isinstance(purpose, str)
    ):
        raise TermoInvalidoError
    return LegalNotices(authorization, privacy, purpose)


@dataclass(frozen=True, slots=True)
class SignedTermo:
    """Minimized metadata extracted from the scanned signed artifact."""

    signed_on: date
    valid_until: date


def unsigned_termo(parties: Parties, draft: TermoDraft) -> bytes:
    """Generate the published tag/attribute structure, omitting Signature entirely."""
    if draft.valid_until < draft.signed_on:
        raise TermoInvalidoError
    nodes = [
        ("sistema", {"id": "API Integra Contador"}),
        ("termo", {"texto": draft.notices.authorization}),
        ("avisoLegal", {"texto": draft.notices.privacy}),
        ("finalidade", {"texto": draft.notices.purpose}),
        ("dataAssinatura", {"data": draft.signed_on.strftime("%Y%m%d")}),
        ("vigencia", {"data": draft.valid_until.strftime("%Y%m%d")}),
        (
            "destinatario",
            {
                "numero": parties.contratante,
                "nome": draft.contratante_name,
                "tipo": "PJ",
                "papel": "contratante",
            },
        ),
        (
            "assinadoPor",
            {
                "numero": parties.firm,
                "nome": draft.firm_name,
                "tipo": "PJ",
                "papel": "autor pedido de dados",
            },
        ),
    ]
    elements: list[str] = []
    for tag, attributes in nodes:
        if any(
            not isinstance(value, str)
            or not value
            or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value)
            for value in attributes.values()
        ):
            raise TermoInvalidoError
        encoded = " ".join(
            f'{name}="{escape(value, quote=True)}"'
            for name, value in attributes.items()
        )
        elements.append(f"<{tag} {encoded}/>")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<termoDeAutorizacao><dados>"
        + "".join(elements)
        + "</dados></termoDeAutorizacao>"
    ).encode()


def _check_signature(root: Element) -> None:
    signature = root.find(f"{{{SIGNATURE_NS}}}Signature")
    if signature is None:
        raise TermoInvalidoError
    for tag in ("SignatureValue", "X509Certificate"):
        value = signature.find(f".//{{{SIGNATURE_NS}}}{tag}")
        if value is None or not value.text:
            raise TermoInvalidoError
        base64.b64decode("".join(value.text.split()), validate=True)


def stored_termo(document: "Document") -> bytes:
    """Read a scanned signed termo's stored bytes, refusing a changed artifact."""
    with default_storage.open(document.storage_key, "rb") as source:
        raw: bytes = source.read(MAX_XML + 1)
    if hashlib.sha256(raw).hexdigest() != document.sha256:
        raise TermoInvalidoError
    return raw


def read_signed_termo(raw: bytes, parties: Parties) -> SignedTermo:
    """Reject oversized/XXE, wrong-firm and unsigned XML before a submission exists.

    The provider validates the ICP-Brasil chain and cryptographic signature.
    This boundary checks structure and attribution, never claims verification.
    """
    if not raw or len(raw) > MAX_XML:
        raise TermoInvalidoError
    try:
        root = ElementTree.fromstring(
            raw, forbid_dtd=True, forbid_entities=True, forbid_external=True
        )
        if root.tag != "termoDeAutorizacao" or [child.tag for child in root] != [
            "dados",
            f"{{{SIGNATURE_NS}}}Signature",
        ]:
            raise TermoInvalidoError
        data = root[0]
        if tuple(child.tag for child in data) != CHILDREN:
            raise TermoInvalidoError
        if data[0].get("id") != "API Integra Contador":
            raise TermoInvalidoError
        recipient, author = data[6], data[7]
        if (
            recipient.get("numero") != parties.contratante
            or author.get("numero") != parties.firm
            or recipient.get("tipo") != "PJ"
            or author.get("tipo") != "PJ"
            or recipient.get("papel") != "contratante"
            or author.get("papel") != "autor pedido de dados"
        ):
            raise TermoInvalidoError
        values = [data[index].get("data", "") for index in (4, 5)]
        if any(re.fullmatch(r"[0-9]{8}", value) is None for value in values):
            raise TermoInvalidoError
        signed_on, valid_until = (date.fromisoformat(value) for value in values)
        _check_signature(root)
        if valid_until < signed_on:
            raise TermoInvalidoError
    except (ElementTree.ParseError, DefusedXmlException, ValueError):
        raise TermoInvalidoError from None
    return SignedTermo(signed_on, valid_until)
