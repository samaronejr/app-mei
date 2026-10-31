"""PAGTOWEB at the SERPRO JSON boundary: page requests, strict pages and receipts.

AXRM-015 (plan todo 15; A6 SP-PAG, SP-COMP). Shapes follow SERPRO's public Integra
Contador documentation for PAGAMENTOS71 ("Consulta Pagamento") and
COMPARRECADACAO72 ("Emite Comprovante Pagamento"), read 2026-10-27:

* A page request carries `intervaloDataArrecadacao` {dataInicial, dataFinal}
  (AAAA-MM-DD), optional `codigoReceitaLista`, `tamanhoDaPagina` (at most 100) and
  `primeiroDaPagina` (zero-based offset).
* A page answer's `dados` is an escaped JSON ARRAY of `DocumentoArrecadacaoIC`:
  `numeroDocumento`, `tipo` {codigo, descricao, descricaoAbreviada},
  `periodoApuracao`/`dataArrecadacao`/`dataVencimento` (AAAA-MM-DDTHH:MI:SS-03:00),
  `receitaPrincipal` {codigo, descricao, extensaoReceita} and JSON-number amounts.
* The document type CODE is authoritative (domain table: 04 DARF, 07 DJE,
  08 DARF SIMPLES, 09 DAS, 10 DAE; the published examples also send it unpadded,
  "9"). The descriptions are display prose and decide nothing.
* A receipt answer's `dados` is the PDF in Base64, documented both as an escaped
  `{"pdf": "<base64>"}` object (detailed example) and as a bare Base64 string
  (field table). Both are accepted; anything else is refused.

Nothing here performs I/O or stores provider text: the wrappers validate the one
transient response of a single call BEFORE the protocol finalizes it, so a
malformed HTTP 200 is a billed failure, never a successful empty consultation.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from django.core.exceptions import ValidationError

from apps.integrations.call_models import CallState
from apps.integrations.transport_types import TransportFailure, decode_json
from apps.obligations.federal import PaymentRecord, payment_from_provider
from apps.obligations.models import FederalPaymentType

if TYPE_CHECKING:
    from apps.integrations.call_contracts import CallResult
    from apps.integrations.call_dispatch import Dispatch
    from apps.integrations.serpro.adapter import SerproAdapter
    from apps.integrations.transport_types import JSONValue, ProviderResponse

PAGE_OPERATION: Final = "SP-PAG"
RECEIPT_OPERATION: Final = "SP-COMP"
PAGE_SIZE: Final = 100
MAX_PAGES: Final = 10
MAX_RANGE_DAYS: Final = 366
MAX_REVENUE_CODES: Final = 20
INVALID: Final = "resposta_pagtoweb_invalida"
# Domain codes, compared without left zero padding. DARF SIMPLES (08) is stored in
# AXRM-014's four-family model as DARF: a deliberate, documented narrowing.
FAMILIES: Final = {
    "4": FederalPaymentType.DARF,
    "7": FederalPaymentType.DJE,
    "8": FederalPaymentType.DARF,
    "9": FederalPaymentType.DAS,
    "10": FederalPaymentType.DAE,
}
_AMOUNTS: Final = ("valorTotal", "valorPrincipal", "valorMulta", "valorJuros")
_PDF_MAGIC: Final = b"%PDF-"


def _invalid(status: int | None = None) -> TransportFailure:
    return TransportFailure(INVALID, status)


@dataclass(frozen=True, slots=True)
class PaymentRange:
    """The arrecadacao window one consultation asks about, plus optional revenues."""

    start: date
    end: date
    revenue_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Refuse an inverted, unbounded or malformed question before reserving."""
        if (
            self.end < self.start
            or self.end - self.start > timedelta(days=MAX_RANGE_DAYS)
            or len(self.revenue_codes) > MAX_REVENUE_CODES
            or any(
                re.fullmatch(r"[0-9]{1,4}", code) is None for code in self.revenue_codes
            )
        ):
            msg = "Intervalo de consulta de pagamentos inválido."
            raise ValidationError(msg)


def page_data(window: PaymentRange, index: int) -> JSONValue:
    """Build the documented PAGAMENTOS71 `dados` for zero-based page `index`."""
    if not 0 <= index < MAX_PAGES:
        msg = "Página fora do limite da consulta."
        raise ValueError(msg)
    data: dict[str, JSONValue] = {
        "intervaloDataArrecadacao": {
            "dataInicial": window.start.isoformat(),
            "dataFinal": window.end.isoformat(),
        },
        "primeiroDaPagina": index * PAGE_SIZE,
        "tamanhoDaPagina": PAGE_SIZE,
    }
    if window.revenue_codes:
        data["codigoReceitaLista"] = list(window.revenue_codes)
    return data


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid()
    return value.strip()


def _amount(item: dict[str, JSONValue], name: str, *, required: bool) -> JSONValue:
    """Amounts are documented JSON numbers; null is only allowed when optional."""
    value = item.get(name)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid()
    return value


def _item(value: JSONValue) -> PaymentRecord:
    """Map one documented `DocumentoArrecadacaoIC` onto todo 14's normalizer."""
    if not isinstance(value, dict):
        raise _invalid()
    tipo = value.get("tipo")
    receita = value.get("receitaPrincipal")
    if not isinstance(tipo, dict) or not isinstance(receita, dict):
        raise _invalid()
    family = FAMILIES.get(_text(tipo.get("codigo")).lstrip("0"))
    if family is None:
        raise _invalid()
    descricao = receita.get("descricao")
    if descricao is not None and not isinstance(descricao, str):
        raise _invalid()
    vencimento = value.get("dataVencimento")
    if vencimento is not None:
        vencimento = _text(vencimento)
    flat: dict[str, object] = {
        "numeroDocumento": _text(value.get("numeroDocumento")),
        "tipo": family,
        "periodoApuracao": _text(value.get("periodoApuracao")),
        "dataArrecadacao": _text(value.get("dataArrecadacao")),
        "dataVencimento": vencimento,
        "receita": {
            "codigo": _text(receita.get("codigo")),
            "descricao": descricao or "",
        },
    }
    for name in _AMOUNTS:
        flat[name] = _amount(value, name, required=name == "valorTotal")
    try:
        return payment_from_provider(flat)
    except ValidationError:
        raise _invalid() from None


def _envelope(response: ProviderResponse) -> str:
    """Return the escaped `dados` of a well-formed envelope for this exact status."""
    try:
        outer = response.json()
    except TransportFailure:
        raise _invalid(response.status) from None
    if not isinstance(outer, dict):
        raise _invalid(response.status)
    status = outer.get("status", response.status)
    messages = outer.get("mensagens", [])
    escaped = outer.get("dados")
    if (
        type(status) is not int
        or status != response.status
        or not isinstance(messages, list)
        or not all(
            isinstance(message, dict) and isinstance(message.get("codigo"), str)
            for message in messages
        )
        or not isinstance(escaped, str)
    ):
        raise _invalid(response.status)
    return escaped


def parse_page(response: ProviderResponse) -> tuple[PaymentRecord, ...]:
    """Parse one complete page, or refuse the whole page; never a partial page.

    HTTP 204 (documented as "sem conteudo", not billed) is an empty page.
    """
    if response.status == 204:  # noqa: PLR2004 - documented empty answer
        return ()
    try:
        data = decode_json(_envelope(response).encode())
    except (ValueError, RecursionError):
        raise _invalid(response.status) from None
    if not isinstance(data, list) or len(data) > PAGE_SIZE:
        raise _invalid(response.status)
    records = tuple(_item(item) for item in data)
    numbers = [record.numero_documento for record in records]
    if len(set(numbers)) != len(numbers):
        raise _invalid(response.status)
    return records


def parse_receipt(response: ProviderResponse) -> bytes:
    """Decode the receipt PDF from either documented `dados` shape, strictly."""
    escaped = _envelope(response)
    text = escaped.strip()
    if text.startswith("{"):
        try:
            data = decode_json(text.encode())
        except (ValueError, RecursionError):
            raise _invalid(response.status) from None
        if not isinstance(data, dict) or set(data) != {"pdf"}:
            raise _invalid(response.status)
        text = data["pdf"] if isinstance(data["pdf"], str) else ""
    try:
        pdf = base64.b64decode("".join(text.split()), validate=True)
    except (binascii.Error, ValueError):
        raise _invalid(response.status) from None
    if not pdf.startswith(_PDF_MAGIC):
        raise _invalid(response.status)
    return pdf


# The normalized fields a kept page stores (`PaymentPage.records`): exactly what
# `FederalPayment` keeps of each document, never the provider's envelope or text.
_KEPT_TEXT: Final = ("numero_documento", "tipo", "receita_codigo", "receita_descricao")
_KEPT_DATES: Final = ("periodo_apuracao", "data_arrecadacao", "data_vencimento")
_KEPT_AMOUNTS: Final = ("valor_total", "valor_principal", "valor_multa", "valor_juros")


def page_rows(records: tuple[PaymentRecord, ...]) -> list[dict[str, str | None]]:
    """Return the stored form of a parsed page: ISO dates, exact decimal strings."""
    return [
        {
            name: None if (value := getattr(record, name)) is None else str(value)
            for name in (*_KEPT_TEXT, *_KEPT_DATES, *_KEPT_AMOUNTS)
        }
        for record in records
    ]


def _kept_day(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def _kept_amount(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def page_records(rows: list[dict[str, str | None]]) -> tuple[PaymentRecord, ...]:
    """Rebuild the parsed page `page_rows` stored, unmatched as it was parsed."""
    return tuple(
        PaymentRecord(
            numero_documento=str(row["numero_documento"]),
            tipo=str(row["tipo"]),
            periodo_apuracao=date.fromisoformat(str(row["periodo_apuracao"])),
            data_arrecadacao=date.fromisoformat(str(row["data_arrecadacao"])),
            data_vencimento=_kept_day(row["data_vencimento"]),
            receita_codigo=str(row["receita_codigo"]),
            receita_descricao=str(row["receita_descricao"]),
            valor_total=Decimal(str(row["valor_total"])),
            valor_principal=_kept_amount(row["valor_principal"]),
            valor_multa=_kept_amount(row["valor_multa"]),
            valor_juros=_kept_amount(row["valor_juros"]),
        )
        for row in rows
    )


class PageAdapter:
    """Validate the transient page before the fenced finalize records success."""

    def __init__(self, adapter: SerproAdapter) -> None:
        """Wrap the one-send SERPRO adapter; parsed records live for this call only."""
        self.adapter = adapter
        self.records: tuple[PaymentRecord, ...] | None = None

    def send(self, dispatch: Dispatch) -> CallResult:
        """Keep a malformed success billed, but finalize it as a symbolic failure."""
        self.records = None
        result = self.adapter.send(dispatch)
        response = self.adapter.response
        if result.state != CallState.SUCCESS or response is None:
            return result
        try:
            self.records = parse_page(response)
        except TransportFailure:
            return replace(result, state=CallState.FAILED, provider_code=INVALID)
        return result


class ReceiptAdapter:
    """Validate the transient receipt PDF before the fenced finalize."""

    def __init__(self, adapter: SerproAdapter) -> None:
        """Wrap the one-send SERPRO adapter; the PDF bytes live for this call only."""
        self.adapter = adapter
        self.pdf: bytes | None = None

    def send(self, dispatch: Dispatch) -> CallResult:
        """Finalize an undecodable receipt as a billed failure, never a document."""
        self.pdf = None
        result = self.adapter.send(dispatch)
        response = self.adapter.response
        if result.state != CallState.SUCCESS or response is None:
            return result
        try:
            self.pdf = parse_receipt(response)
        except TransportFailure:
            return replace(result, state=CallState.FAILED, provider_code=INVALID)
        return result
