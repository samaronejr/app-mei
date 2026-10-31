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
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.serpro.models import (
    ACTIVE_QUERY_STATES,
    PaymentQuery,
    PaymentQueryState,
)
from apps.integrations.serpro.payments import CHECKS, FLAG, receipt_request_key
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


def query_detail(query: PaymentQuery) -> str:  # noqa: PLR0911 - one per state
    """Explain one consultation without inferring anything about non-payment."""
    pages = _plural(query.pages_fetched, "página obtida", "páginas obtidas")
    match query.state:
        case PaymentQueryState.NA_FILA | PaymentQueryState.EM_ANDAMENTO:
            return "Consulta em andamento. A página é atualizada ao recarregar."
        case PaymentQueryState.ENCONTRADO:
            found = _plural(
                query.payments_found, "pagamento encontrado", "pagamentos encontrados"
            )
            matched = _plural(
                query.matched_count, "associado a DAS", "associados a DAS"
            )
            return f"{found} ({pages}); {matched}."
        case PaymentQueryState.NADA_LOCALIZADO:
            return (
                "Nenhum pagamento localizado no intervalo consultado. "
                "Isso não indica falta de pagamento."
            )
        case PaymentQueryState.SEM_PROCURACAO:
            return (
                "Sem procuração eletrônica (código 00004) para este cliente. "
                "Consulta não realizada; registre e verifique a procuração."
            )
        case PaymentQueryState.COTA_ESGOTADA:
            return (
                "Limite mensal de consultas atingido. Nenhuma consulta foi feita "
                "depois disso."
            )
        case PaymentQueryState.INCERTO:
            return "O resultado é incerto. Confira com a equipe antes de repetir."
        case _ if query.failure_code == "limite_de_paginas":
            return (
                f"A consulta atingiu o limite de 10 páginas ({pages}); "
                "o resultado pode estar incompleto."
            )
        case _:
            return f"A consulta não foi concluída ({pages}, mantidas)."


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
    return FederalPaymentsPanel(
        period=period,
        queries=[
            QueryRow(
                query=query,
                label=PaymentQueryState(query.state).label,
                badge=QUERY_BADGES.get(query.state, ""),
                detail=query_detail(query),
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
