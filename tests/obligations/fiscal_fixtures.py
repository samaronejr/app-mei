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
