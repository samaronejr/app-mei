"""The "Receita Federal" payments section of the workspace and the cycle (plan A9).

Firm-side only and drawn only while `federal_checks` is on. Honest vocabulary
(A7.7): a payment reads as confirmed only when it came from a `producao` call with
the owner's real-data approval and was associated with a DAS obligation; simulated,
trial and homologation answers say so. An empty answer is "nada localizado", never
"not paid". Constant query count: consultations, payments, their receipt calls
and the receipt documents, whatever the number of rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from django.conf import settings

from apps.authz.services import can
from apps.core.flags import tenant_has
from apps.integrations.call_models import CallState, ProviderCall, ProviderCallAttempt
from apps.integrations.serpro.denials import (
    DenialCause,
    denial_cause,
    denial_text,
    is_denial_code,
)
from apps.integrations.serpro.models import (
    ACTIVE_QUERY_STATES,
    PaymentQuery,
    PaymentQueryState,
)
from apps.integrations.serpro.payments import (
    CHECKS,
    FIRM_REFUSALS,
    FLAG,
    page_numbers,
    receipt_request_key,
)
from apps.integrations.serpro.work import (
    ADMISSION_REFUSED,
    BLOCK_PREFIX,
    CLEARED_PREFIX,
    CLIENT_ABSENT,
    ISSUE_PREFIX,
    firm_reason,
    frozen_code,
)
from apps.obligations.models import (
    Document,
    FederalEnvironment,
    FederalPayment,
    FederalPaymentType,
)

if TYPE_CHECKING:
    from datetime import date

    from apps.accounts.models import User
    from apps.clients.models import ClientCompany

HISTORY: Final = 5
# A firm-side admission refusal: never the client's procuracao (AXRM-045).


class PaymentBadge(StrEnum):
    """The partials/ui/status_badge.html states this section draws."""

    CONFIRMED = auto()
    PENDING = auto()
    UNKNOWN = auto()
    BLOCKED = auto()
    DANGER = auto()


QUERY_BADGES: Final[dict[str, str]] = {
    PaymentQueryState.NA_FILA: PaymentBadge.PENDING,
    PaymentQueryState.EM_ANDAMENTO: PaymentBadge.PENDING,
    PaymentQueryState.SEM_PROCURACAO: PaymentBadge.BLOCKED,
    PaymentQueryState.COTA_ESGOTADA: PaymentBadge.BLOCKED,
    PaymentQueryState.FALHA: PaymentBadge.DANGER,
    PaymentQueryState.INCERTO: PaymentBadge.UNKNOWN,
}
RECEIPT_LABELS: Final[dict[str, str]] = {
    CallState.RESERVED: "Emissão do comprovante na fila",
    CallState.CLAIMED: "Emissão do comprovante em andamento",
    CallState.STARTED: "Emissão do comprovante em andamento",
    CallState.DENIED: "Sem procuração (código 00004): comprovante não emitido",
    CallState.QUOTA: "Limite de consultas atingido: comprovante não emitido",
    CallState.AUTHORITY: "Emissão bloqueada pela autorização",
    CallState.PAUSED: "Integração pausada: comprovante não emitido",
    CallState.UNCERTAIN: "Resultado da emissão incerto: confira antes de repetir",
    CallState.FAILED: "Falha na emissão do comprovante",
}
COLUMNS: Final[tuple[dict[str, object], ...]] = (
    {"label": "Documento"},
    {"label": "Tipo"},
    {"label": "Pago em"},
    {"label": "Valor total", "numeric": True},
    {"label": "Situação"},
    {"label": "Comprovante"},
)


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def firm_refusal(code: str) -> bool:
    """Whether a failure code names a firm-side admission refusal."""
    return code in FIRM_REFUSALS or firm_reason(code) or code.startswith(CLEARED_PREFIX)


def firm_refusal_text(query: PaymentQuery) -> str:
    """Name a firm-side refusal by the code recorded with it, without page facts."""
    code = query.failure_code
    if code == "termo_ausente":
        return (
            "A consulta parou: não havia termo de autorização do escritório em "
            f"vigor; {UNCHANGED}."
        )
    if code == ADMISSION_REFUSED:
        return f"A admissão recusou a consulta sem registrar a causa; {UNCHANGED}."
    cause = code.removeprefix(CLEARED_PREFIX)
    denial = frozen_code(cause) or (
        cause.removeprefix(BLOCK_PREFIX) if cause.startswith(BLOCK_PREFIX) else ""
    )
    if denial:
        kind = f"código {denial}"
    elif cause.startswith(ISSUE_PREFIX):
        kind = "pendência sem código 403 registrado"
    else:
        kind = f"registro {cause}"
    return (
        f"A admissão recusou a consulta por um bloqueio do escritório ({kind}); "
        f"{UNCHANGED}."
    )


UNCHANGED: Final = "a procuração do cliente não foi alterada"


def _client_refusal_text(query: PaymentQuery) -> str:
    """Name the client's refusal by the code recorded with it, without page facts."""
    code = query.failure_code
    if code != CLIENT_ABSENT:
        return (
            "A Receita respondeu por falta da procuração eletrônica do cliente "
            f"(código {code})."
        )
    return (
        "A admissão recusou a consulta: a procuração eletrônica do cliente (00004) "
        "não estava em vigor."
    )


@dataclass(frozen=True, slots=True)
class PageFact:
    """What one page's call and its attempts record: sent, billed, refused."""

    number: int
    sent: bool
    billed: str
    refused: bool
    code: str = ""


# The budget's own refusal reasons, each stated exactly. A cap reason does not
# record whether the firm's or the platform's ceiling refused (AXRM-045).
QUOTA_REASONS: Final = {
    "cota_esgotada": "o limite mensal de consultas acabou",
    "cota_ausente": "não há limite mensal de consultas configurado",
    "tarifa_ausente": "não há tarifa cadastrada para esta consulta",
    "tarifa_invalida": "a tarifa cadastrada para esta consulta é inválida",
    "reserva_ausente": "a reserva de custo da página não foi encontrada",
    "reserva_vencida": "a reserva de custo da página venceu antes do envio",
}
# Recorded by older releases without the budget's reason.
LEGACY_QUOTA: Final = "cota_esgotada_parcial"
QUOTA_LABEL: Final = "Recusada pelo controle de custos"
# An attempt's definitive observation; replay may also leave a provisional one.
DEFINITIVE: Final = "result"


def _attempt_charge(rows: list[ProviderCallAttempt]) -> str:
    """Decide one attempt's charge by meaning, never by the order rows are read.

    Its definitive result decides; without one, a recorded charge is never lost
    to a provisional unknown (AXRM-045).
    """
    definitive = [row.billed for row in rows if row.observation == DEFINITIVE]
    if definitive:
        return definitive[0]
    charges = {row.billed for row in rows}
    if "sim" in charges:
        return "sim"
    return "desconhecido" if "desconhecido" in charges else "nao"


def _fact(
    number: int, call: ProviderCall, attempts: list[ProviderCallAttempt]
) -> PageFact:
    """Read one page: sent if its call started a dispatch or recorded any attempt.

    A retry's claim clears the call's dispatch start, so an earlier attempt
    (a 401, say) is still a send when the retry is then refused (AXRM-045).
    """
    by_attempt: dict[object, list[ProviderCallAttempt]] = {}
    for attempt in attempts:
        by_attempt.setdefault(attempt.attempt_id, []).append(attempt)
    charges = [_attempt_charge(rows) for rows in by_attempt.values()]
    if call.dispatch_started_at is not None:
        charges.append(call.billed)
    if "sim" in charges:
        billed = "sim"
    elif "desconhecido" in charges:
        billed = "desconhecido"
    else:
        billed = "nao"
    refused = call.state == CallState.QUOTA
    return PageFact(number, bool(charges), billed, refused, call.provider_code)


def page_calls(queries: list[PaymentQuery]) -> dict[object, list[PageFact]]:
    """Each consultation's recorded page facts by page number, in two lookups."""
    owners = {
        key: (query.pk, number)
        for query in queries
        for key, number in page_numbers(query).items()
    }
    calls = list(
        ProviderCall.objects.filter(
            client_id__in={query.client_id for query in queries},
            request_key__in=list(owners),
        )
    )
    attempts: dict[object, list[ProviderCallAttempt]] = {call.pk: [] for call in calls}
    for attempt in ProviderCallAttempt.objects.filter(call_id__in=list(attempts)):
        attempts[attempt.call_id].append(attempt)
    found: dict[object, list[PageFact]] = {query.pk: [] for query in queries}
    for call in calls:
        owner, number = owners[call.request_key]
        found[owner].append(_fact(number, call, attempts[call.pk]))
    for pages in found.values():
        pages.sort(key=lambda page: page.number)
    return found


def _pages(numbers: list[int]) -> str:
    if len(numbers) == 1:
        return f"a página {numbers[0]}"
    if numbers == list(range(numbers[0], numbers[-1] + 1)):
        joined = f"{numbers[0]} a {numbers[-1]}"
        if len(numbers) == 2:  # noqa: PLR2004 - a pair reads "1 e 2"
            joined = f"{numbers[0]} e {numbers[1]}"
    else:
        joined = ", ".join(map(str, numbers[:-1])) + f" e {numbers[-1]}"
    return f"as páginas {joined}"


def page_facts(pages: list[PageFact]) -> str:
    """State which pages were sent and billed, from the recorded page calls only.

    A page is billed only when a call or attempt records a charge: a 204 or a
    401 is sent without one (AXRM-045).
    """
    sent = [page.number for page in pages if page.sent]
    if not sent:
        return "Nenhuma página foi enviada."
    billing = {page.number: page.billed for page in pages if page.sent}
    billed = [number for number in sent if billing[number] == "sim"]
    unknown = [number for number in sent if billing[number] == "desconhecido"]
    free = [number for number in sent if number not in billed + unknown]
    one = len(sent) == 1
    text = f"{_pages(sent)} {'foi enviada' if one else 'foram enviadas'}"
    text = f"{text[0].upper()}{text[1:]}"
    if billed == sent:
        return f"{text} e {'cobrada' if one else 'cobradas'}."
    if not billed and not unknown:
        return f"{text}, sem cobrança."
    parts = []
    if billed:
        verb = "foi cobrada" if len(billed) == 1 else "foram cobradas"
        parts.append(f"{_pages(billed)} {verb}")
    if unknown:
        parts.append(f"a cobrança não é conhecida para {_pages(unknown)}")
    if free:
        parts.append(f"{_pages(free)} sem cobrança")
    return f"{text}: {'; '.join(parts)}."


def quota_refusal(query: PaymentQuery, pages: list[PageFact]) -> bool:
    """Whether the budget's refusal is what closed the consultation.

    Only the cause it was closed with counts: a budget code, or the code a
    refused page call records. A stale runner's later refusal never does.
    """
    code = query.failure_code
    return query.state == PaymentQueryState.COTA_ESGOTADA or (
        query.state == PaymentQueryState.FALHA
        and (
            code in QUOTA_REASONS
            or code == LEGACY_QUOTA
            or any(page.refused and page.code == code for page in pages)
        )
    )


def _quota_text(query: PaymentQuery, pages: list[PageFact]) -> str:
    """Name the budget's own recorded reason, and the page or retry it refused."""
    code = query.failure_code
    cited = [page for page in pages if page.refused and page.code == code]
    refused = cited or [page for page in pages if page.refused]
    if not refused:
        which = "a consulta"
    elif refused[-1].sent:
        which = f"a nova tentativa da página {refused[-1].number}"
    else:
        which = _pages([refused[-1].number])
    refusal = f"O controle de custos recusou {which} antes do envio"
    if code in QUOTA_REASONS:
        return f"{refusal}: {QUOTA_REASONS[code]}."
    return f"{refusal} (registro {code})."


def query_label(query: PaymentQuery) -> str:
    """Name the row's badge: only a spent cap reads as the limit reached."""
    if query.state == PaymentQueryState.COTA_ESGOTADA and (
        query.failure_code != "cota_esgotada"
    ):
        return QUOTA_LABEL
    return PaymentQueryState(query.state).label


def query_detail(query: PaymentQuery, pages: list[PageFact]) -> str:
    """Explain one ended consultation, then which of its pages were sent and billed.

    The page facts come from the consultation's recorded page calls and their
    attempts, never from its page counter (AXRM-045).
    """
    if query.state in ACTIVE_QUERY_STATES:
        return "Consulta em andamento. A página é atualizada ao recarregar."
    if quota_refusal(query, pages):
        return f"{_quota_text(query, pages)} {page_facts(pages)}"
    return f"{_outcome(query)} {page_facts(pages)}"


def _outcome(query: PaymentQuery) -> str:  # noqa: PLR0911 - one per state
    """Explain one consultation without inferring anything about non-payment."""
    code = query.failure_code
    match query.state:
        case PaymentQueryState.ENCONTRADO:
            found = _plural(
                query.payments_found, "pagamento encontrado", "pagamentos encontrados"
            )
            matched = _plural(
                query.matched_count, "associado a DAS", "associados a DAS"
            )
            return f"{found}; {matched}."
        case PaymentQueryState.NADA_LOCALIZADO:
            return (
                "Nenhum pagamento localizado no intervalo consultado. "
                "Isso não indica falta de pagamento."
            )
        case PaymentQueryState.SEM_PROCURACAO:
            return _client_refusal_text(query)
        case PaymentQueryState.FALHA if firm_refusal(code):
            return firm_refusal_text(query)
        case PaymentQueryState.FALHA | PaymentQueryState.INCERTO if is_denial_code(
            code
        ):
            return f"{denial_text(code)} Consulta não repetida."
        case PaymentQueryState.INCERTO:
            return (
                f"O resultado é incerto (registro {code}). Confira com a equipe "
                "antes de repetir."
            )
        case _ if code == "limite_de_paginas":
            return (
                "A consulta atingiu o limite de 10 páginas; o resultado pode estar "
                "incompleto."
            )
        case _:
            return f"A consulta não foi concluída (registro {code})."


@dataclass(frozen=True, slots=True)
class QueryRow:
    """One consultation in the history."""

    query: PaymentQuery
    label: str
    badge: str
    detail: str
    environment_label: str


@dataclass(frozen=True, slots=True)
class PaymentRow:
    """One payment, its association and its receipt state."""

    payment: FederalPayment
    tipo_label: str
    status_label: str
    badge: str
    receipt_label: str
    may_request_receipt: bool


@dataclass(frozen=True, slots=True)
class FederalPaymentsPanel:
    """Everything the section draws for one client and competence."""

    period: date
    queries: list[QueryRow]
    matched: list[PaymentRow]
    unmatched: list[PaymentRow]
    may_run: bool
    running: bool
    latest_id: str
    nonce: str
    columns: tuple[dict[str, object], ...] = COLUMNS


def _environment_label(environment: str) -> str:
    try:
        return FederalEnvironment(environment).label
    except ValueError:
        return "Ambiente desconhecido"


def _official(payment: FederalPayment, environment: str) -> bool:
    return (
        payment.matched_obligation_id is not None
        and environment == FederalEnvironment.PRODUCAO
        and settings.PROVIDER_REAL_DATA_APPROVED is True
    )


def _status(payment: FederalPayment, environment: str) -> tuple[str, str]:
    if _official(payment, environment):
        return PaymentBadge.CONFIRMED, "Pagamento encontrado na Receita"
    if payment.matched_obligation_id is not None:
        return (
            PaymentBadge.UNKNOWN,
            (
                f"Associado ao DAS ({_environment_label(environment)}: "
                "não confirma pagamento)"
            ),
        )
    return PaymentBadge.UNKNOWN, "Não associado a obrigação"


def _receipt(call: ProviderCall | None, document: Document | None) -> str:
    if call is None:
        return "Não solicitado"
    if call.state == CallState.SUCCESS:
        if document is None:
            return "Comprovante emitido, mas não armazenado"
        return "Comprovante armazenado (somente equipe; verificação antivírus)"
    if call.state == CallState.DENIED and (
        denial_cause(call.provider_code) != DenialCause.PROCURACAO_CLIENTE
    ):
        return f"{denial_text(call.provider_code)} Comprovante não emitido."
    return RECEIPT_LABELS.get(call.state, "Situação do comprovante desconhecida")


def federal_payments_panel(
    user: User, client: ClientCompany, period: date
) -> FederalPaymentsPanel | None:
    """Build the section for a client the caller already loaded through its portfolio.

    None while `federal_checks` is off: the section is not drawn at all.
    """
    if not tenant_has(client.tenant_id, FLAG):
        return None
    queries = list(
        PaymentQuery.objects.filter(client_id=client.pk).order_by("-created_at", "-pk")[
            :HISTORY
        ]
    )
    payments = list(
        FederalPayment.objects.filter(client_id=client.pk, periodo_apuracao=period)
        .select_related("consultation")
        .order_by("data_arrecadacao", "numero_documento")
    )
    keys = {receipt_request_key(payment.pk): payment.pk for payment in payments}
    calls = {
        keys[call.request_key]: call
        for call in ProviderCall.objects.filter(
            client_id=client.pk, request_key__in=list(keys)
        )
    }
    documents = {
        document.acquisition_receipt_ref: document
        for document in Document.objects.filter(
            client_id=client.pk,
            acquisition_receipt_ref__in=[str(call.pk) for call in calls.values()],
        )
    }
    may_run = can(user, CHECKS)
    rows: list[PaymentRow] = []
    for payment in payments:
        environment = payment.consultation.environment
        badge, status = _status(payment, environment)
        call = calls.get(payment.pk)
        rows.append(
            PaymentRow(
                payment=payment,
                tipo_label=FederalPaymentType(payment.tipo).label,
                status_label=status,
                badge=badge,
                receipt_label=_receipt(
                    call, documents.get(str(call.pk)) if call else None
                ),
                may_request_receipt=may_run and call is None,
            )
        )
    running = bool(queries) and queries[0].state in ACTIVE_QUERY_STATES
    calls_by_query = page_calls(queries)
    return FederalPaymentsPanel(
        period=period,
        queries=[
            QueryRow(
                query=query,
                label=query_label(query),
                badge=QUERY_BADGES.get(query.state, ""),
                detail=query_detail(query, calls_by_query[query.pk]),
                environment_label=_environment_label(query.environment),
            )
            for query in queries
        ],
        matched=[row for row in rows if row.payment.matched_obligation_id],
        unmatched=[row for row in rows if not row.payment.matched_obligation_id],
        may_run=may_run and not running,
        running=running,
        latest_id=str(queries[0].pk) if queries else "",
        nonce=uuid4().hex,
    )
