"""Projection-only ISS rule shared by fiscal ingest and the historical backfill."""

from typing import Final

from apps.cycles.parsing import parse_xml

UNKNOWN_PARTY: Final = "desconhecido"
PARTY_BY_CODE: Final = {"1": "nao_retido", "2": "tomador", "3": "intermediario"}
ISS_SOURCE_SPAN: Final = "DPS/infDPS/valores/trib/tribMun/tpRetISSQN"
_NFSE: Final = "{http://www.sped.fazenda.gov.br/nfse}"
_PATH: Final = "/".join(
    _NFSE + element for element in f"infNFSe/{ISS_SOURCE_SPAN}".split("/")
)


def corroborated_iss_code(payload: bytes, retained_code: str | None) -> str | None:
    """Check accepted original bytes without changing v2's normalized field."""
    if retained_code not in PARTY_BY_CODE:
        return None
    root = parse_xml(payload)
    if root.tag != f"{_NFSE}NFSe" or root.get("versao") not in ("1.00", "1.01"):
        return None
    elements = root.findall(_PATH)
    if (
        len(elements) == 1
        and not len(elements[0])
        and elements[0].text == retained_code
    ):
        return retained_code
    return None


def project_iss_retention(code: str | None) -> tuple[str, bool | None]:
    """Translate a source code; unknown can never assert non-retention."""
    party = (
        PARTY_BY_CODE.get(code, UNKNOWN_PARTY) if code is not None else UNKNOWN_PARTY
    )
    return party, None if party == UNKNOWN_PARTY else party != "nao_retido"
