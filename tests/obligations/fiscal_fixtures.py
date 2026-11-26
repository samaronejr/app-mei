"""Synthetic fiscal XML; no taxpayer document or provider response is retained."""

KEY = "33" + "0" * 42
CLIENT = "11222333000181"
COUNTERPART = "11444777000161"
ALPHANUMERIC = "12ABC34501DE35"


def nfe(
    *,
    recipient: str = CLIENT,
    issuer: str = COUNTERPART,
    total: str = "1500.25",
    state: str = "100",
) -> bytes:
    """An authorized NF-e wrapper with independent issuer and recipient."""
    return (
        '<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe" versao="4.00">'
        f'<NFe><infNFe Id="NFe{KEY}" versao="4.00">'
        "<ide><dhEmi>2026-09-15T12:30:00-03:00</dhEmi></ide>"
        f"<emit><CNPJ>{issuer}</CNPJ><xNome>Synthetic issuer</xNome></emit>"
        f"<dest><CNPJ>{recipient}</CNPJ><xNome>Synthetic recipient</xNome></dest>"
        f"<total><ICMSTot><vNF>{total}</vNF></ICMSTot></total>"
        "</infNFe></NFe>"
        f"<protNFe><infProt><cStat>{state}</cStat></infProt></protNFe></nfeProc>"
    ).encode()


def summary(*, total: str = "1500.25", state: str = "1") -> dict[str, str]:
    """A resNFe has an issuer only; the capture binding supplies its audience."""
    return {
        "chNFe": KEY,
        "CNPJ": COUNTERPART,
        "xNome": "Synthetic issuer",
        "dhEmi": "2026-09-15T12:30:00-03:00",
        "vNF": total,
        "cSitNFe": state,
    }


def nfse(*, version: str = "1.01", extra: str = "") -> bytes:
    """National NFS-e shape from the official 1.00/1.01 XSD, with stated ISS."""
    return (
        f'<NFSe xmlns="http://www.sped.fazenda.gov.br/nfse" versao="{version}">'
        f'<infNFSe Id="NFS{KEY}"><cLocIncid>3303302</cLocIncid>'
        f"<emit><CNPJ>{ALPHANUMERIC}</CNPJ><xNome>Synthetic prestador</xNome></emit>"
        "<valores><vISSQN>12.34</vISSQN></valores>"
        '<DPS versao="1.00"><infDPS Id="DPSsynthetic">'
        "<dhEmi>2026-09-15T12:30:00-03:00</dhEmi><dCompet>2026-09-01</dCompet>"
        f"<prest><CNPJ>{ALPHANUMERIC}</CNPJ></prest>"
        f"<toma><CNPJ>{CLIENT}</CNPJ><xNome>Synthetic tomador</xNome></toma>"
        "<valores><vServPrest><vServ>800.00</vServ></vServPrest>"
        "<trib><tribMun><tpRetISSQN>2</tpRetISSQN></tribMun></trib></valores>"
        f"{extra}</infDPS></DPS></infNFSe></NFSe>"
    ).encode()


def retention_element(code: str | None) -> bytes:
    """Build the exact XML element, or omit it completely."""
    return f"<tpRetISSQN>{code}</tpRetISSQN>".encode() if code is not None else b""


def nfse_with_retention(element: bytes) -> bytes:
    return nfse().replace(retention_element("2"), element)


UNTRUSTED_ISS_ELEMENTS = [
    ("mixed", retention_element("1<invalid/>3"), "1"),
    ("padded", retention_element(" 2 "), "2"),
    ("padded-unknown", retention_element(" 9 "), "9"),
    ("nbsp", retention_element("&#160;1&#160;"), "1"),
    ("duplicate", retention_element("2") + retention_element("3"), "2"),
    ("same-duplicate", retention_element("2") * 2, "2"),
    ("unknown", retention_element("9"), "9"),
    ("absent", b"", None),
    ("empty", retention_element(""), None),
    ("blank", retention_element(" \n\t "), None),
]
LEGACY_ISS_DIGEST = {
    "1": "5a1484c78d39e67f4de40baaf9a9f2d595119dd7390affb1a80f743e2e5a4236",
    "2": "432a8f92f97c2db78227ca7274b6aa9049996135ed923d57554cbbddc9a80368",
    "3": "727d55fc31436728dc981bcfa265484cd45bec4837dbb7fe67fb543450d40760",
    "9": "1ec3baa0de88b1f27f228e4653ffda651b712dabc1dc671b77b334381c937245",
    None: "eef28260ad24b706acc70305bab8880cd52e517156968a8c8ba9a39a23228670",
}
