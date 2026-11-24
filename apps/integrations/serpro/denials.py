"""Classify a SERPRO 403 by its documented reason code (A6 "SERPRO 403 causes").

The mapping is closed: every code the official message table (api-integra-contador
/pt/mensagens/, read 2026-11-22) gives as HTTP 403 has one cause, and any other
code is `desconhecida`. Only the bounded code is kept, never the message text.
"""

import re
from collections.abc import Iterable
from typing import Final

from django.db import models

UNPARSED: Final = "serpro_http_403"
_SHAPE: Final = re.compile(r"[A-Za-z]{1,24}-ICGERENCIADOR-[0-9]{3}")


class DenialCause(models.TextChoices):
    """Who owns the fix for one documented 403 (A6)."""

    PROCURACAO_CLIENTE = "procuracao_cliente", "Procuração do cliente"
    TERMO_ESCRITORIO = "termo_escritorio", "Termo de autorização do escritório"
    CREDENCIAL_PLATAFORMA = "credencial_plataforma", "Credenciais da plataforma"
    SERVICO_NAO_ELEGIVEL = "servico_nao_elegivel", "Serviço não elegível"
    FALHA_DO_PROVEDOR = "falha_do_provedor", "Falha do sistema de procurações"
    DESCONHECIDA = "desconhecida", "Código não catalogado"


_CODES: Final[dict[DenialCause, tuple[str, ...]]] = {
    DenialCause.PROCURACAO_CLIENTE: (
        "AcessoNegado-ICGERENCIADOR-022",
        "Erro-ICGERENCIADOR-032",
    ),
    DenialCause.TERMO_ESCRITORIO: (
        "AcessoNegado-ICGERENCIADOR-019",
        "AcessoNegado-ICGERENCIADOR-020",
        "AcessoNegado-ICGERENCIADOR-053",
        "AcessoNegado-ICGERENCIADOR-054",
    ),
    DenialCause.CREDENCIAL_PLATAFORMA: tuple(
        f"AcessoNegado-ICGERENCIADOR-{number:03d}"
        for number in (3, 4, 5, 13, 14, 15, 16, 18, 25, 26, 27, 37, 38, 41, 42, 45)
    ),
    DenialCause.SERVICO_NAO_ELEGIVEL: ("AcessoNegado-ICGERENCIADOR-021",),
    DenialCause.FALHA_DO_PROVEDOR: (
        "Erro-ICGERENCIADOR-031",
        "AcessoNegado-ICGERENCIADOR-033",
    ),
}
CAUSES: Final[dict[str, DenialCause]] = {
    code: cause for cause, codes in _CODES.items() for code in codes
}
# The platform operator owns these (A6): repeating such a call is billed and useless.
PLATFORM_CAUSES: Final = frozenset(
    {DenialCause.CREDENCIAL_PLATAFORMA, DenialCause.SERVICO_NAO_ELEGIVEL}
)
# Causes the firm or the platform owns: never a client's work (AXRM-045).
FIRM_CAUSES: Final = frozenset({DenialCause.TERMO_ESCRITORIO, *PLATFORM_CAUSES})
DENIAL_TEXT: Final[dict[str, str]] = {
    DenialCause.PROCURACAO_CLIENTE: (
        "Sem procuração eletrônica do cliente para este serviço na Receita"
    ),
    DenialCause.TERMO_ESCRITORIO: (
        "A Receita recusou o termo de autorização do escritório; a procuração "
        "do cliente não foi alterada"
    ),
    DenialCause.CREDENCIAL_PLATAFORMA: (
        "A Receita recusou as credenciais da plataforma; as chamadas ao SERPRO "
        "estão suspensas até a operação corrigir"
    ),
    DenialCause.SERVICO_NAO_ELEGIVEL: (
        "A Receita informou que o serviço não aceita procuração eletrônica; as "
        "chamadas ao SERPRO estão suspensas até a operação revisar o catálogo"
    ),
    DenialCause.FALHA_DO_PROVEDOR: (
        "O sistema de procurações da Receita falhou; nada foi alterado e a "
        "próxima execução agendada ou um pedido da equipe tenta de novo"
    ),
    DenialCause.DESCONHECIDA: (
        "A Receita recusou com um código não catalogado; o resultado é incerto "
        "e nada é repetido automaticamente"
    ),
}


def _bounded(code: str) -> str | None:
    """Return the documented code without its brackets, or None for anything else."""
    if code.startswith("[") and code.endswith("]"):
        code = code[1:-1]
    return code if _SHAPE.fullmatch(code) else None


def denial_code(codes: Iterable[str]) -> str:
    """Keep the catalogued code a 403 carries, else its first well-formed one."""
    shaped = [code for code in map(_bounded, codes) if code is not None]
    for code in shaped:
        if code in CAUSES:
            return code
    return shaped[0] if shaped else UNPARSED


def denial_cause(code: str) -> DenialCause:
    """Map through the closed A6 table; an unlisted code is never a client fault."""
    return CAUSES.get(code, DenialCause.DESCONHECIDA)


def is_denial_code(code: str) -> bool:
    """Whether a stored failure code is the bounded code of a SERPRO 403."""
    return code == UNPARSED or _SHAPE.fullmatch(code) is not None


def result_cause(state: str, code: str) -> str:
    """Return the cause a provider result carries: a SERPRO denial's, else ''."""
    return str(denial_cause(code)) if state == "negada" and is_denial_code(code) else ""


def denial_text(code: str) -> str:
    """pt-BR copy for one stored 403 code, with the code itself shown."""
    return f"{DENIAL_TEXT[denial_cause(code)]} (código {code})."
