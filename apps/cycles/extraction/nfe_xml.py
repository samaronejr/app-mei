"""NF-e fields from the portalfiscal namespace: parties, number, date, total.

Takes the element `apps.cycles.parsing` already parsed (defusedxml, DTDs forbidden).
Returns None when the root is not an NF-e, so the caller can try the next layout.
"""

from typing import Final
from xml.etree.ElementTree import Element

from apps.cycles.evidence import FieldOrigin
from apps.cycles.extraction import LAYOUT_UNKNOWN, Extraction, FieldValue
from apps.cycles.extraction.cnpj import party
from apps.fiscal.validators import normalize_document

NS: Final = "{http://www.portalfiscal.inf.br/nfe}"
VALUE_MAX: Final = 255


def _text(node: Element, path: str) -> str:
    found = node.find(path)
    if found is None or found.text is None:
        return ""
    return found.text.strip()[:VALUE_MAX]


def extract(root: Element) -> Extraction | None:
    """Read an NF-e; None when the root is outside the NF-e namespace."""
    if not root.tag.startswith(NS):
        return None
    info = root if root.tag == f"{NS}infNFe" else root.find(f".//{NS}infNFe")
    if info is None:
        return Extraction(kind="nfe", note=LAYOUT_UNKNOWN)
    paths = {
        "emitente_cnpj": f"{NS}emit/{NS}CNPJ",
        "destinatario_cnpj": f"{NS}dest/{NS}CNPJ",
        "numero": f"{NS}ide/{NS}nNF",
        "data_emissao": f"{NS}ide/{NS}dhEmi",
        "valor_total": f"{NS}total/{NS}ICMSTot/{NS}vNF",
    }
    fields: list[FieldValue] = []
    parties: list[str] = []
    for name, path in paths.items():
        value = _text(info, path)
        if name == "data_emissao" and not value:
            value = _text(info, f"{NS}ide/{NS}dEmi")
        if not value:
            continue
        if name.endswith("_cnpj"):
            value = normalize_document(value)
            if party(value):
                parties.append(value)
        if name == "data_emissao":
            value = value[:10]
        span = path.replace(NS, "")
        fields.append(FieldValue(name, value, FieldOrigin.XML, span))
    return Extraction(kind="nfe", fields=tuple(fields), parties=tuple(parties))
