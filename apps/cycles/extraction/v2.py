"""Fiscal extraction v2; portal uploads retain the independent R1 extractor.

The official production XSD bundle NFSe-ESQUEMAS_XSD-v1.01-20260209 accepts
XML versions 1.00 and 1.01. RTC NT009 v1.01 changes the IBS/CBS groups, whose
contents are deliberately outside this extractor's stated-ISS contract.
"""

from dataclasses import dataclass
from typing import Final
from xml.etree.ElementTree import Element

from apps.cycles.evidence import DocumentIntake, FieldOrigin
from apps.cycles.extraction import FieldValue
from apps.cycles.parsing import UnsafeDocument, parse_intake_xml
from apps.fiscal.validators import normalize_document

EXTRACTOR_VERSION: Final = "2"
KNOWN_VERSIONS: Final = frozenset({"1.00", "1.01"})
MAX_BYTES: Final = 5 * 1024 * 1024
VALUE_MAX: Final = 255
PARTY_LENGTHS: Final = {"CNPJ": 14, "CPF": 11}
NFE: Final = "{http://www.portalfiscal.inf.br/nfe}"
NFSE: Final = "{http://www.sped.fazenda.gov.br/nfse}"
STATE: Final = {
    "100": "autorizada",
    "150": "autorizada",
    "101": "cancelada",
    "151": "cancelada",
    "110": "denegada",
    "301": "denegada",
    "302": "denegada",
}
SUMMARY_STATE: Final = {
    "1": "autorizada",
    "2": "denegada",
    "3": "cancelada",
}


@dataclass(frozen=True, slots=True)
class FiscalExtraction:
    """Identity and suggestions read only from a clean original."""

    family: str
    key_namespace: str
    document_key: str
    fiscal_state: str
    fields: tuple[FieldValue, ...] = ()
    summary_only: bool = False
    note: str = ""


def _text(info: Element, path: str) -> str:
    return (info.findtext(path) or "").strip()


def _fields(
    info: Element, namespace: str, paths: dict[str, tuple[str, ...]]
) -> tuple[FieldValue, ...]:
    fields: list[FieldValue] = []
    for name, alternatives in paths.items():
        for path in alternatives:
            qualified = "/".join(namespace + part for part in path.split("/"))
            value = _text(info, qualified)
            if not value:
                continue
            if len(value) > VALUE_MAX:
                code = "field_too_long"
                raise UnsafeDocument(code)
            if name.endswith("_id"):
                value = normalize_document(value)
                tag = path.rsplit("/", 1)[-1]
                if (
                    len(value) != PARTY_LENGTHS[tag]
                    or not value.isascii()
                    or not value.isalnum()
                    or (tag == "CPF" and not value.isdecimal())
                ):
                    code = "invalid_party_id"
                    raise UnsafeDocument(code)
            if name == "issue_date":
                value = value[:10]
            fields.append(FieldValue(name, value, FieldOrigin.XML, path))
            break
    return tuple(fields)


def extract(intake: DocumentIntake, payload: bytes) -> FiscalExtraction:
    """Read only clean, capped XML; DTD/entity refusals propagate to the caller."""
    if len(payload) > MAX_BYTES:
        code = "too_large"
        raise UnsafeDocument(code)
    root = parse_intake_xml(intake, payload)
    if root.tag == f"{NFE}resNFe":
        return FiscalExtraction(
            family="nfe",
            key_namespace="sefaz",
            document_key=_text(root, f"{NFE}chNFe"),
            fiscal_state=SUMMARY_STATE.get(
                _text(root, f"{NFE}cSitNFe"), "desconhecida"
            ),
            fields=_fields(
                root,
                NFE,
                {
                    "emitente_id": ("CNPJ", "CPF"),
                    "emitente_name": ("xNome",),
                    "issue_date": ("dhEmi",),
                    "total": ("vNF",),
                },
            ),
            summary_only=True,
        )
    info = root if root.tag == f"{NFE}infNFe" else root.find(f".//{NFE}infNFe")
    if info is not None and root.tag.startswith(NFE):
        identifier = info.get("Id", "")
        if not identifier.startswith("NFe"):
            code = "missing_fiscal_key"
            raise UnsafeDocument(code)
        return FiscalExtraction(
            family="nfe",
            key_namespace="sefaz",
            document_key=identifier[3:],
            fiscal_state=STATE.get(
                _text(root, f".//{NFE}infProt/{NFE}cStat"), "desconhecida"
            ),
            fields=_fields(
                info,
                NFE,
                {
                    "emitente_id": ("emit/CNPJ", "emit/CPF"),
                    "destinatario_id": ("dest/CNPJ", "dest/CPF"),
                    "emitente_name": ("emit/xNome",),
                    "destinatario_name": ("dest/xNome",),
                    "issue_date": ("ide/dhEmi", "ide/dEmi"),
                    "total": ("total/ICMSTot/vNF",),
                },
            ),
        )
    info = root.find(f"{NFSE}infNFSe")
    if root.tag != f"{NFSE}NFSe" or info is None:
        code = "unsupported_fiscal_xml"
        raise UnsafeDocument(code)
    key = info.get("Id", "")
    if root.get("versao") not in KNOWN_VERSIONS:
        return FiscalExtraction(
            "nfse_nacional",
            "nacional",
            key,
            "desconhecida",
            note="layout nao reconhecido",
        )
    return FiscalExtraction(
        family="nfse_nacional",
        key_namespace="nacional",
        document_key=key,
        fiscal_state="autorizada",
        fields=_fields(
            info,
            NFSE,
            {
                "prestador_id": (
                    "emit/CNPJ",
                    "emit/CPF",
                    "DPS/infDPS/prest/CNPJ",
                    "DPS/infDPS/prest/CPF",
                ),
                "tomador_id": ("DPS/infDPS/toma/CNPJ", "DPS/infDPS/toma/CPF"),
                "prestador_name": ("emit/xNome",),
                "tomador_name": ("DPS/infDPS/toma/xNome",),
                "issue_date": ("DPS/infDPS/dhEmi",),
                "competence": ("DPS/infDPS/dCompet",),
                "municipality_incidence": ("cLocIncid",),
                "total": ("DPS/infDPS/valores/vServPrest/vServ",),
                "iss_stated": ("valores/vISSQN",),
                "iss_withheld_flag": ("DPS/infDPS/valores/trib/tribMun/tpRetISSQN",),
            },
        ),
    )
