"""AXRM-015 PAGTOWEB wire contract: documented requests, strict pages and receipts."""

import base64
import json
from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.integrations.call_contracts import CallResult
from apps.integrations.serpro.pagtoweb import (
    INVALID,
    MAX_PAGES,
    PageAdapter,
    PaymentRange,
    ReceiptAdapter,
    page_data,
    parse_page,
    parse_receipt,
)
from apps.integrations.transport_types import ProviderResponse, TransportFailure
from tests.integrations.pagtoweb_support import PDF, item, page_body, receipt_body

WINDOW = PaymentRange(date(2026, 9, 1), date(2026, 10, 27))


def response(body: bytes, status: int = 200) -> ProviderResponse:
    return ProviderResponse(status, body)


def test_page_request_is_the_documented_dados_with_zero_based_offsets() -> None:
    first, third = page_data(WINDOW, 0), page_data(WINDOW, 2)
    assert first == {
        "intervaloDataArrecadacao": {
            "dataInicial": "2026-09-01",
            "dataFinal": "2026-10-27",
        },
        "primeiroDaPagina": 0,
        "tamanhoDaPagina": 100,
    }
    assert isinstance(third, dict)
    assert third["primeiroDaPagina"] == 200
    coded = page_data(PaymentRange(WINDOW.start, WINDOW.end, ("1001", "0190")), 0)
    assert isinstance(coded, dict)
    assert coded["codigoReceitaLista"] == ["1001", "0190"]
    with pytest.raises(ValueError, match="limite"):
        page_data(WINDOW, MAX_PAGES)


@pytest.mark.parametrize(
    ("start", "end", "codes"),
    [
        (date(2026, 10, 2), date(2026, 10, 1), ()),
        (date(2025, 1, 1), date(2026, 10, 27), ()),
        (date(2026, 9, 1), date(2026, 9, 30), ("12345",)),
        (date(2026, 9, 1), date(2026, 9, 30), ("10a1",)),
    ],
    ids=["inverted", "over-a-year", "long-code", "non-numeric-code"],
)
def test_malformed_questions_are_refused_before_any_reservation(
    start: date, end: date, codes: tuple[str, ...]
) -> None:
    with pytest.raises(ValidationError):
        PaymentRange(start, end, codes)


def test_every_documented_field_normalizes_with_decimal_through_str() -> None:
    documented = item(
        "07082207696788000",
        tipo="9",
        periodo="2026-09-01T00:00:00-03:00",
        total=66.6,
        principal=60.1,
        multa=4.3,
        juros=2.2,
    )
    documented["receitaPrincipal"]["descricao"] = None
    [record] = parse_page(response(page_body([documented])))
    assert record.numero_documento == "07082207696788000"
    assert record.tipo == "DAS"
    assert record.periodo_apuracao == date(2026, 9, 1)
    assert record.data_arrecadacao == date(2026, 10, 20)
    assert record.data_vencimento == date(2026, 10, 20)
    assert record.receita_codigo == "1001"
    assert record.receita_descricao == ""
    # 66.6 as a binary float is 66.599999...; the str conversion keeps the cent.
    assert (record.valor_total, record.valor_principal) == (
        Decimal("66.60"),
        Decimal("60.10"),
    )
    assert (record.valor_multa, record.valor_juros) == (
        Decimal("4.30"),
        Decimal("2.20"),
    )
    assert record.matched_obligation_id is None


@pytest.mark.parametrize(
    ("code", "family"),
    [
        ("04", "DARF"),
        ("4", "DARF"),
        ("07", "DJE"),
        ("08", "DARF"),
        ("09", "DAS"),
        ("9", "DAS"),
        ("10", "DAE"),
    ],
)
def test_the_type_code_decides_the_family_never_the_description(
    code: str, family: str
) -> None:
    documented = item("1", tipo=code)
    documented["tipo"]["descricaoAbreviada"] = "DAS"
    [record] = parse_page(response(page_body([documented])))
    assert record.tipo == family


def test_optional_amounts_stay_null_and_a_period_end_date_is_its_month() -> None:
    documented = item("2", tipo="04", periodo="2026-08-31T00:00:00-03:00")
    documented.update(valorPrincipal=None, dataVencimento=None)
    [record] = parse_page(response(page_body([documented])))
    assert record.periodo_apuracao == date(2026, 8, 1)
    assert record.valor_principal is None
    assert record.data_vencimento is None


def test_empty_array_and_documented_204_are_empty_pages() -> None:
    assert parse_page(response(page_body([]))) == ()
    assert parse_page(response(b"", 204)) == ()


def _envelope(status: int, dados: object) -> bytes:
    return json.dumps(
        {"status": status, "mensagens": [], "dados": json.dumps(dados)}
    ).encode()


def _changed(**fields: object) -> bytes:
    base = item("3")
    for name, value in fields.items():
        if value is _DROP:
            del base[name]
        else:
            base[name] = value
    return page_body([base])


_DROP = object()
_TIPO = {"codigo": "99", "descricao": "x", "descricaoAbreviada": "x"}
MALFORMED = {
    "not-a-list": _envelope(200, {"pagamentos": []}),
    "oversized-page": page_body([item(str(n)) for n in range(101)]),
    "duplicate-document": page_body([item("9"), item("9")]),
    "unknown-type": _changed(tipo=_TIPO),
    "type-as-string": _changed(tipo="DAS"),
    "string-amount": _changed(valorTotal="76.60"),
    "bool-amount": _changed(valorTotal=True),
    "missing-total": _changed(valorTotal=_DROP),
    "over-precision": _changed(valorTotal=76.601),
    "negative": _changed(valorTotal=-1),
    "bad-date": _changed(dataArrecadacao="20/10/2026"),
    "missing-period": _changed(periodoApuracao=_DROP),
    "missing-revenue": _changed(receitaPrincipal=None),
    "numeric-document": _changed(numeroDocumento=123),
    "status-mismatch": _envelope(403, [item("3")]),
    "duplicate-key": (
        b'{"status":200,"mensagens":[],"dados":"[]","dados":'
        + json.dumps(json.dumps([item("3")])).encode()
        + b"}"
    ),
    "nan": page_body([item("3")]).replace(b"76.6", b"NaN"),
    "not-json": b"<html>ok</html>",
}


@pytest.mark.parametrize("change", sorted(MALFORMED))
def test_a_malformed_page_is_refused_whole_with_a_symbolic_code(change: str) -> None:
    with pytest.raises(TransportFailure) as raised:
        parse_page(response(MALFORMED[change]))
    assert raised.value.code == INVALID
    assert "76" not in str(raised.value)


def test_receipt_accepts_both_documented_shapes_strictly() -> None:
    encoded = base64.b64encode(PDF).decode()
    assert parse_receipt(response(receipt_body(json.dumps({"pdf": encoded})))) == PDF
    assert parse_receipt(response(receipt_body(encoded))) == PDF
    for bad in (
        json.dumps({"pdf": encoded, "extra": 1}),
        json.dumps({"pdf": "@@@"}),
        base64.b64encode(b"not a pdf").decode(),
        "",
    ):
        with pytest.raises(TransportFailure):
            parse_receipt(response(receipt_body(bad)))


class _Inner:
    def __init__(self, result: CallResult, body: bytes) -> None:
        self.result = result
        self.response: ProviderResponse | None = ProviderResponse(200, body)

    def send(self, _dispatch: object) -> CallResult:
        return self.result


def test_a_malformed_success_finalizes_as_a_billed_failure() -> None:
    success = CallResult("sucesso", 200, "serpro_http_200", "sim")
    page = PageAdapter(_Inner(success, b"{}"))  # type: ignore[arg-type]
    receipt = ReceiptAdapter(_Inner(success, b"{}"))  # type: ignore[arg-type]
    for adapter in (page, receipt):
        result = adapter.send(None)  # type: ignore[arg-type]
        assert (result.state, result.billed, result.provider_code) == (
            "falhou",
            "sim",
            INVALID,
        )
    assert page.records is None
    assert receipt.pdf is None
    good = PageAdapter(_Inner(success, page_body([item("4")])))  # type: ignore[arg-type]
    assert good.send(None).state == "sucesso"  # type: ignore[arg-type]
    assert good.records is not None
    assert len(good.records) == 1
