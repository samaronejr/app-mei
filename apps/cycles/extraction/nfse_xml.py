"""NFS-e national layout (Padrão Nacional, sped.fazenda.gov.br/nfse) fields.

Only the versions listed in `KNOWN_VERSIONS` are read. Any other version, or a root in
the namespace without the expected shape, yields empty fields and "layout não
reconhecido": a reviewer reads the document instead of trusting a guess.
"""

from typing import Final
from xml.etree.ElementTree import Element

from apps.cycles.evidence import FieldOrigin
from apps.cycles.extraction import LAYOUT_UNKNOWN, Extraction, FieldValue
from apps.cycles.extraction.cnpj import party
from apps.fiscal.validators import normalize_document

NS: Final = "{http://www.sped.fazenda.gov.br/nfse}"
KNOWN_VERSIONS: Final = frozenset({"1.00", "1.01"})
VALUE_MAX: Final = 255
DPS: Final = f"{NS}DPS/{NS}infDPS"
PATHS: Final[dict[str, tuple[str, ...]]] = {
    "prestador_cnpj": (f"{NS}emit/{NS}CNPJ", f"{DPS}/{NS}prest/{NS}CNPJ"),
    "tomador_cnpj": (f"{DPS}/{NS}toma/{NS}CNPJ",),
    "numero": (f"{NS}nNFSe",),
    "competencia": (f"{DPS}/{NS}dCompet",),
    "data_emissao": (f"{DPS}/{NS}dhEmi",),
    "valor_servico": (f"{DPS}/{NS}valores/{NS}vServPrest/{NS}vServ",),
}


def _first(node: Element, paths: tuple[str, ...]) -> tuple[str, str]:
    for path in paths:
        found = node.find(path)
        if found is not None and found.text and found.text.strip():
            return found.text.strip()[:VALUE_MAX], path.replace(NS, "")
    return "", ""


def extract(root: Element) -> Extraction | None:
    """Read a national NFS-e; None when the root is outside its namespace."""
    if not root.tag.startswith(NS):
        return None
    info = root.find(f"{NS}infNFSe")
    if (
        root.tag != f"{NS}NFSe"
        or root.get("versao") not in KNOWN_VERSIONS
        or info is None
    ):
        return Extraction(kind="nfse", note=LAYOUT_UNKNOWN)
    fields: list[FieldValue] = []
    parties: list[str] = []
    for name, paths in PATHS.items():
        value, span = _first(info, paths)
        if not value:
            continue
        if name.endswith("_cnpj"):
            value = normalize_document(value)
            if party(value):
                parties.append(value)
        elif name == "competencia":
            value = value[:7]
        elif name == "data_emissao":
            value = value[:10]
        fields.append(FieldValue(name, value, FieldOrigin.XML, span))
    return Extraction(kind="nfse", fields=tuple(fields), parties=tuple(parties))
