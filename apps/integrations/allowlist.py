"""A6's closed wire contract. No registration hook can widen this allowlist."""

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal
from urllib.parse import urlencode

from apps.integrations.transport_types import (
    JSONValue,
    OperacaoNaoPermitida,
    Provider,
    ProviderRequest,
    decode_json,
)


@dataclass(frozen=True, slots=True)
class Route:
    """An exact method/path and its upstream effect and media class."""

    method: Literal["GET", "POST", "PUT"]
    path: str
    endpoint_class: Literal["read", "issuance", "registration", "configuration"]
    media: Literal["json", "xml", "pdf"] = "json"


SERPRO_SERVICES: Final = MappingProxyType(
    {
        "SP-TERMO": ("AUTENTICAPROCURADOR", "ENVIOXMLASSINADO81"),
        "SP-SIT-P": ("SITFIS", "SOLICITARPROTOCOLO91"),
        "SP-SIT-R": ("SITFIS", "RELATORIOSITFIS92"),
        "SP-PAG": ("PAGTOWEB", "PAGAMENTOS71"),
        "SP-COMP": ("PAGTOWEB", "COMPARRECADACAO72"),
        "SP-DAS": ("PGMEI", "GERARDASPDF21"),
    }
)
# A6 families with more than one route use named variants, never arbitrary paths.
ROUTES: Final = MappingProxyType(
    {
        ("SP-TERMO", "default"): Route("POST", "/Apoiar", "registration"),
        ("SP-SIT-P", "default"): Route("POST", "/Apoiar", "issuance"),
        ("SP-SIT-R", "default"): Route("POST", "/Emitir", "issuance"),
        ("SP-PAG", "default"): Route("POST", "/Consultar", "read"),
        ("SP-COMP", "default"): Route("POST", "/Emitir", "issuance"),
        ("SP-DAS", "default"): Route("POST", "/Emitir", "issuance"),
        ("FC-EMP", "default"): Route("GET", "/v2/empresas", "configuration"),
        ("FC-EMP", "create"): Route("POST", "/v2/empresas", "configuration"),
        ("FC-EMP", "update"): Route("PUT", "/v2/empresas/{subject}", "configuration"),
        ("FC-EMP", "read"): Route("GET", "/v2/empresas/{subject}", "configuration"),
        ("FC-NFE", "default"): Route("GET", "/v2/nfes_recebidas", "read"),
        ("FC-NFE", "xml"): Route(
            "GET", "/v2/nfes_recebidas/{subject}.xml", "read", "xml"
        ),
        ("FC-NFE", "cce"): Route(
            "GET", "/v2/nfes_recebidas/{subject}/carta_correcao.xml", "read", "xml"
        ),
        ("FC-NFS", "default"): Route("GET", "/v2/nfsens_recebidas", "read"),
        ("FC-NFS", "xml"): Route(
            "GET", "/v2/nfsens_recebidas/{subject}.xml", "read", "xml"
        ),
        ("FC-CIE", "default"): Route(
            "POST", "/v2/nfes_recebidas/{subject}/manifesto", "registration"
        ),
        ("FC-PDF", "default"): Route(
            "GET", "/v2/nfes_recebidas/{subject}.pdf", "read", "pdf"
        ),
        ("FC-PDF", "nfse"): Route(
            "GET", "/v2/nfsens_recebidas/{subject}.pdf", "read", "pdf"
        ),
    }
)


def _body(code: str, request: ProviderRequest) -> None:
    try:
        body: JSONValue = decode_json(request.body)
    except (ValueError, RecursionError):
        raise OperacaoNaoPermitida from None
    if not isinstance(body, dict):
        raise OperacaoNaoPermitida
    if code in SERPRO_SERVICES:
        pedido = body.get("pedidoDados")
        if (
            not isinstance(pedido, dict)
            or (pedido.get("idSistema"), pedido.get("idServico"))
            != SERPRO_SERVICES[code]
            or pedido.get("versaoSistema") != "1.0"
        ):
            raise OperacaoNaoPermitida
    elif code == "FC-CIE":
        if body != {"tipo": "ciencia"}:
            raise OperacaoNaoPermitida
    elif code == "FC-EMP":
        # Never relay certificates or unknown provider switches.
        allowed = {
            "cnpj",
            "nome",
            "nome_fantasia",
            "inscricao_estadual",
            "inscricao_municipal",
            "regime_tributario",
            "logradouro",
            "numero",
            "complemento",
            "bairro",
            "cep",
            "municipio",
            "uf",
            "email",
            "habilita_nfe_recebidas",
            "habilita_nfsen_recebidas_producao",
            "habilita_nfsen_recebidas_homologacao",
            "data_inicio_recebimento_nfe",
            "data_inicio_recebimento_nfsen",
            "habilita_filtro_mde_destinatario_empresa",
            "certificado_especifico",
        }
        if body.keys() - allowed:
            raise OperacaoNaoPermitida


def resolve(
    provider: Provider, code: str, request: ProviderRequest
) -> tuple[Route, str]:
    """Refuse unknown ids, route variants, path escapes and disguised operations."""
    route = ROUTES.get((code, request.variant))
    if route is None or code.startswith("SP-") != (provider == "serpro"):
        raise OperacaoNaoPermitida
    if "{subject}" in route.path:
        pattern = r"[0-9]{1,20}" if code == "FC-EMP" else r"[0-9]{44,50}"
        if re.fullmatch(pattern, request.subject) is None:
            raise OperacaoNaoPermitida
    elif request.subject:
        raise OperacaoNaoPermitida
    keys = [key for key, _ in request.query]
    if len(keys) != len(set(keys)) or set(keys) - {"cnpj", "versao"}:
        raise OperacaoNaoPermitida
    if request.query and (provider != "focus" or route.method != "GET"):
        raise OperacaoNaoPermitida
    if route.method == "GET":
        if request.body:
            raise OperacaoNaoPermitida
    else:
        _body(code, request)
    _headers(request)
    path = route.path.format(subject=request.subject)
    return route, path + ("?" + urlencode(request.query) if request.query else "")


def _headers(request: ProviderRequest) -> None:
    allowed_headers = {
        "authorization",
        "jwt_token",
        "autenticar_procurador_token",
        "x-request-tag",
        "content-type",
        "accept",
    }
    names = [name.lower() for name, _ in request.headers]
    if len(names) != len(set(names)) or set(names) - allowed_headers:
        raise OperacaoNaoPermitida
    if any(re.search(r"[\x00-\x1f\x7f]", value) for _, value in request.headers):
        raise OperacaoNaoPermitida
