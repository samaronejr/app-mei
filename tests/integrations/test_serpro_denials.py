"""AXRM-045: a SERPRO 403 is classified by its documented reason code (A6, RS-2).

Table-driven over every HTTP 403 code of the official message table (fixture read
2026-11-22): cause, owner, effect on the client's authority, billing and copy.
Only the client-procuracao group demotes an authority; a firm-termo or platform
code opens the firm issue, which refuses that firm's next calls; a provider
failure demotes nothing; an unknown code is `incerto`. No 403 is ever re-sent.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest
from django.db.models import F
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.integrations.authority import head_record
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.call_contracts import AdmissionDeniedError
from apps.integrations.call_models import ProviderCall
from apps.integrations.models import (
    ClientProviderLink,
    IntegrationIssue,
    ProviderConnection,
)
from apps.integrations.serpro import payments
from apps.integrations.serpro.authorization import TermoSubmission, submit_termo
from apps.integrations.serpro.models import FirmIntegrationIssue, PaymentQuery
from apps.integrations.serpro.payments_panel import federal_payments_panel as panel
from apps.integrations.serpro.payments_panel import query_detail
from apps.integrations.transport_types import ProviderResponse
from apps.obligations.models import FederalConsultation, FederalPayment
from tests.fakes.provider import Reply
from tests.integrations import pagtoweb_support as support
from tests.integrations.pagtoweb_support import World, denied, page, world
from tests.integrations.serpro_support import serpro_module
from tests.integrations.test_pagtoweb import receipts
from tests.integrations.test_pagtoweb_ownership import lease_runs_out

__all__ = ["world"]
# Only the flows touch the database; the table and code checks are pure.
DB: Final = pytest.mark.django_db(transaction=True)


def history_row(world: World, query: PaymentQuery) -> Any:  # noqa: ANN401
    """The client page's row for one consultation, as staff read it."""
    with tenant_context(query.tenant_id):
        client = ClientCompany.objects.get(pk=query.client_id)
        section = panel(world.termo.actor, client, support.COMPETENCE)
    assert section is not None
    [row] = [row for row in section.queries if row.query.pk == query.pk]
    return row


def history(world: World, query: PaymentQuery) -> str:
    """The client page's sentence for one consultation."""
    return str(history_row(world, query).detail)


FIXTURE: Final = Path(__file__).parents[1] / "fixtures" / "providers" / "serpro"
TABLE: Final = json.loads((FIXTURE / "mensagens_gerenciador.json").read_text())
PAGE: Final = (
    "https://apicenter.estaleiro.serpro.gov.br/documentacao/api-integra-contador/pt/"
    "mensagens/"
)
CLIENT: Final = "procuracao_cliente"
TERMO: Final = "termo_escritorio"
UNKNOWN: Final = "desconhecida"
# The A6 table (read 2026-11-15), restated as this test's independent oracle.
EXPECTED: Final[dict[str, tuple[str, ...]]] = {
    CLIENT: ("022", "032"),
    TERMO: ("019", "020", "053", "054"),
    "credencial_plataforma": tuple(
        f"{n:03d}"
        for n in (3, 4, 5, 13, 14, 15, 16, 18, 25, 26, 27, 37, 38, 41, 42, 45)
    ),
    "servico_nao_elegivel": ("021",),
    "falha_do_provedor": ("031", "033"),
}
CAUSE_OF: Final = {
    number: cause for cause, numbers in EXPECTED.items() for number in numbers
}
DENIALS: Final = [row for row in TABLE["messages"] if row["http_status"] == 403]
# Every other cause ends `falha`; these three also stop the firm's next calls.
ENDING: Final = {CLIENT: "sem_procuracao", UNKNOWN: "incerto"}
FIRM_STOPS: Final = frozenset({TERMO, "credencial_plataforma", "servico_nao_elegivel"})
C22: Final = "AcessoNegado-ICGERENCIADOR-022"
TERMO_020: Final = "AcessoNegado-ICGERENCIADOR-020"
A07: Final = "Aviso-ICGERENCIADOR-007"
U98: Final = "Erro-ICGERENCIADOR-098"
U99: Final = "AcessoNegado-ICGERENCIADOR-099"
UNPARSED: Final = "serpro_http_403"


def _code(row: dict[str, Any]) -> str:
    return str(row["code"])


def _number(code: str) -> str:
    return code.rsplit("-", 1)[-1]


def denials_api() -> ModuleType:
    """Assert the classification exists before importing, so the red is an assert."""
    assert find_spec("apps.integrations.serpro.denials") is not None
    return import_module("apps.integrations.serpro.denials")


def body_403(*codes: str, text: str = "synthetic") -> bytes:
    return json.dumps(
        {
            "status": 403,
            "mensagens": [{"codigo": code, "texto": text} for code in codes],
            "dados": "",
        }
    ).encode()


def effects(
    world: World, client: ClientCompany | None = None
) -> tuple[list[str], str, list[str], list[list[str]]]:
    """Receipt outcomes, the PAGTOWEB head, absence issues and firm-issue refs."""
    with tenant_context(world.termo.tenant.pk):
        head = head_record((client or world.termo.client).pk, "pagtoweb")
        assert head is not None
        absent = IntegrationIssue.objects.filter(reason="autorizacao_ausente")
        return (
            list(FederalConsultation.objects.values_list("outcome", flat=True)),
            str(head.status),
            list(absent.values_list("service", flat=True)),
            list(
                FirmIntegrationIssue.objects.filter(
                    reason="autorizacao_procurador_ausente"
                ).values_list("evidence_refs", flat=True)
            ),
        )


def test_the_fixture_is_the_official_table_and_each_403_code_has_one_cause() -> None:
    # Given the official message table as read on its date.
    assert (TABLE["page"], TABLE["read_on"]) == (PAGE, "2026-11-22")
    assert all(
        row["code"] and row["text"] and type(row["http_status"]) is int
        for row in TABLE["messages"]
    )
    # Then its 403 codes are exactly the A6 groups: none left out, none invented.
    assert sorted(_number(_code(row)) for row in DENIALS) == sorted(CAUSE_OF)
    mapped = denials_api().CAUSES
    assert {code: str(cause) for code, cause in mapped.items()} == {
        _code(row): CAUSE_OF[_number(_code(row))] for row in DENIALS
    }


@pytest.mark.parametrize("bracketed", [False, True], ids=["bare", "bracketed"])
@pytest.mark.parametrize("row", DENIALS, ids=_code)
def test_each_documented_403_keeps_only_its_bounded_code(
    row: dict[str, Any], *, bracketed: bool
) -> None:
    # Given a complete 403 carrying one documented code and its official text.
    code = _code(row)
    wire = f"[{code}]" if bracketed else code
    result = serpro_module("adapter").classify(
        "SP-PAG", ProviderResponse(403, body_403(wire, text=row["text"]))
    )
    # Then a billed denial keeps the code alone, and the code names one cause.
    assert (result.state, result.billed, result.provider_code) == (
        "negada",
        "sim",
        code,
    )
    assert row["text"] not in repr(result)
    assert denials_api().denial_cause(code) == CAUSE_OF[_number(code)]
    assert getattr(result, "denial_cause", "") == CAUSE_OF[_number(code)]


@pytest.mark.parametrize(
    "operation", ["SP-TERMO", "SP-SIT-P", "SP-SIT-R", "SP-PAG", "SP-COMP", "SP-DAS"]
)
def test_the_adapter_returns_the_cause_with_the_code_for_every_operation(
    operation: str,
) -> None:
    adapter = serpro_module("adapter")
    denial = adapter.classify(operation, ProviderResponse(403, body_403(TERMO_020)))
    other = adapter.classify(operation, ProviderResponse(500, body_403(TERMO_020)))
    assert (denial.provider_code, getattr(denial, "denial_cause", "")) == (
        TERMO_020,
        TERMO,
    )
    assert getattr(other, "denial_cause", None) == ""


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        pytest.param(body_403(U99), U99, id="unlisted"),
        pytest.param(body_403(f"[{A07}]"), A07, id="unlisted-bracketed"),
        pytest.param(body_403(U98, U99), U98, id="first-unlisted"),
        pytest.param(body_403(U99, f"[{TERMO_020}]"), TERMO_020, id="catalogued"),
        pytest.param(body_403("Erro-Procuracao-00004"), UNPARSED, id="service-code"),
        pytest.param(body_403("Bearer synthetic-token"), UNPARSED, id="text"),
        pytest.param(body_403(f"[{C22}"), UNPARSED, id="open-bracket"),
        pytest.param(body_403(f"x{C22}]"), UNPARSED, id="close-bracket"),
        pytest.param(body_403(f"{C22}2"), UNPARSED, id="long-number"),
        pytest.param(body_403(), UNPARSED, id="no-code"),
        pytest.param(
            b'{"mensagens":["x",{"codigo":7},{"texto":"y"},{"codigo":"%s"}]}'
            % TERMO_020.encode(),
            TERMO_020,
            id="mixed-messages",
        ),
        pytest.param(b'{"mensagens":"%s"}' % C22.encode(), UNPARSED, id="text-list"),
        pytest.param(b'{"mensagens":7}', UNPARSED, id="number-list"),
        pytest.param(b'[{"codigo":"%s"}]' % C22.encode(), UNPARSED, id="array"),
        pytest.param(b"<html>%s</html>" % C22.encode(), UNPARSED, id="html"),
        pytest.param(b"", UNPARSED, id="empty"),
    ],
)
def test_a_403_keeps_a_catalogued_code_first_and_never_text(
    raw: bytes, stored: str
) -> None:
    result = serpro_module("adapter").classify("SP-PAG", ProviderResponse(403, raw))
    assert (result.state, result.billed, result.provider_code) == (
        "negada",
        "sim",
        stored,
    )
    expected = TERMO if stored == TERMO_020 else UNKNOWN
    assert denials_api().denial_cause(stored) == expected


@DB
@pytest.mark.parametrize("row", DENIALS, ids=_code)
def test_every_documented_403_reaches_only_its_own_owner(
    world: World, row: dict[str, Any]
) -> None:
    code = _code(row)
    cause = CAUSE_OF[_number(code)]
    # Given an admitted client whose consultation SERPRO refuses with this code.
    world.script(denied(code=code))
    query = world.consult()
    # Then one billed call, kept with its bounded code whatever the cause ...
    [call] = world.calls()
    assert (call.state, call.billed, call.http_status, call.provider_code) == (
        "negada",
        "sim",
        403,
        code,
    )
    assert world.budget().known_liability == support.CONSULTA_MICROBRL
    ending = ENDING.get(cause, "falha")
    assert (query.state, query.failure_code) == (ending, code)
    # ... and only the owner the code names is touched.
    assert effects(world) == (
        [ending],
        "ausente_detectada" if cause == CLIENT else "ativa",
        ["pagtoweb"] if cause == CLIENT else [],
        [[str(call.pk)]] if cause in FIRM_STOPS else [],
    )
    # And the copy says "Sem procuracao" for the client's own procuracao only.
    detail = history(world, query)
    assert ("falta da procuração" in detail) is (cause == CLIENT)
    if cause != CLIENT:
        expected = denials_api().DENIAL_TEXT[cause]
        assert detail == (
            f"{expected} (código {code}). Consulta não repetida. "
            "A página 1 foi enviada e cobrada."
        )
    # When a person asks again: nothing is re-sent automatically, and a cause
    # whose owner has not acted refuses before any byte.
    world.script(page([]))
    rerun = world.consult(nonce="consulta-2")
    if cause in {"falha_do_provedor", UNKNOWN}:
        assert (rerun.state, len(world.sent())) == ("nada_localizado", 2)
    else:
        assert len(world.sent()) == 1


@DB
def test_a_firm_termo_403_leaves_every_client_authority_and_opens_the_firm_issue(
    world: World,
) -> None:
    # Given a second client of the same firm, with its own verified 00004.
    termo = world.termo
    with tenant_context(termo.tenant.pk):
        other = ClientCompany.objects.create(
            tenant=termo.tenant, legal_name="Outro", cnpj="11444777000161", is_mei=True
        )
        ClientProviderLink.objects.create(
            tenant=termo.tenant,
            client=other,
            connection_id=termo.connection.pk,
            provider_company_ref="synthetic-pagtoweb-b",
            status="ativa",
        )
    support.declare_and_verify(replace(termo, client=other))
    world.script(denied(code=TERMO_020), page([]))
    # When SERPRO refuses the firm's procurador token for the first client.
    query = world.consult()
    [call] = world.calls()
    # Then the firm owns the fix and neither client's authority changed.
    assert (query.state, query.failure_code) == ("falha", TERMO_020)
    assert effects(world)[1:] == ("ativa", [], [[str(call.pk)]])
    assert effects(world, other)[1] == "ativa"
    # And the firm's next call, for the other client, sends nothing and blames
    # the firm, never the other client's procuracao (no absence issue for it).
    refused = payments.consult_payments(
        other, support.WINDOW, actor=None, nonce="consulta-b"
    )
    assert len(world.sent()) == 1
    # The refusal names the block: the firm issue's 020, with its own code.
    assert (refused.state, refused.failure_code) == (
        "falha",
        first_issue(world, TERMO_020),
    )
    detail = history(world, refused)
    assert "procuração do cliente não foi alterada" in detail
    assert TERMO_020 in detail
    assert effects(world, other)[1:3] == ("ativa", [])
    # Neither client gets client work for the firm's denial or the firm's block.
    with tenant_context(termo.tenant.pk):
        assert not IntegrationIssue.objects.exists()


@DB
def test_an_unknown_code_is_incerto_shown_and_never_resent(world: World) -> None:
    world.script(denied(code=U99), page([]))
    query = world.consult()
    assert (query.state, query.failure_code) == ("incerto", U99)
    assert effects(world) == (["incerto"], "ativa", [], [])
    assert U99 in history(world, query)
    assert payments.run_payment_query(query.tenant_id, query.pk).state == "incerto"
    assert len(world.sent()) == 1


@DB
def test_a_firm_termo_403_on_a_receipt_is_firm_work_with_its_own_label(
    world: World,
) -> None:
    # Given one DAS payment found by a successful consultation.
    world.script(page([support.item("DAS000000000045")]))
    world.consult()
    with tenant_context(world.termo.tenant.pk):
        das = FederalPayment.objects.get()
    # When its receipt is refused for the firm's termo, and the click is repeated.
    world.script(denied(code=TERMO_020), page([]))
    tenant, actor = world.termo.tenant.pk, world.termo.actor
    call = payments.emit_payment_receipt(tenant, das.pk, actor.pk)
    assert call is not None
    assert (call.state, call.billed, call.provider_code) == ("negada", "sim", TERMO_020)
    assert payments.emit_payment_receipt(tenant, das.pk, actor.pk) == call
    # Then the firm owns it, the client's authority holds, and the label says so.
    assert effects(world)[1:] == ("ativa", [], [[str(call.pk)]])
    with tenant_context(tenant):
        section = panel(actor, world.termo.client, support.COMPETENCE)
    assert section is not None
    label = (
        "A Receita recusou o termo de autorização do escritório; a procuração do "
        f"cliente não foi alterada (código {TERMO_020}). Comprovante não emitido."
    )
    assert [row.receipt_label for row in section.matched] == [label]
    assert len(world.sent("/Emitir")) == 1


@pytest.mark.parametrize(
    ("state", "code", "detail"),
    [
        (
            "falha",
            "limite_de_paginas",
            "A consulta atingiu o limite de 10 páginas",
        ),
        (
            "falha",
            "resposta_pagtoweb_invalida",
            "A consulta não foi concluída (registro resposta_pagtoweb_invalida).",
        ),
        (
            "incerto",
            "pagina_nao_registrada",
            "O resultado é incerto (registro pagina_nao_registrada).",
        ),
        (
            "falha",
            "termo_ausente",
            "A consulta parou: não havia termo de autorização do escritório",
        ),
    ],
    ids=["page-limit", "failure", "uncertain", "firm-termo"],
)
def test_a_failure_that_is_not_a_403_keeps_its_own_copy(
    state: str, code: str, detail: str
) -> None:
    query = PaymentQuery(state=state, failure_code=code, pages_fetched=10)
    assert query_detail(query, []).startswith(detail)


def test_a_call_result_refuses_a_cause_that_is_not_a_closed_code() -> None:
    contracts = import_module("apps.integrations.call_contracts")
    fields = contracts.CallResult.__dataclass_fields__
    assert "denial_cause" in fields
    # A denial code requires its own closed cause; None is never a cause.
    for missing in (None, "", "falha_do_provedor"):
        with pytest.raises(ValueError, match="Invalid minimized provider result"):
            contracts.CallResult("negada", 403, TERMO_020, "sim", denial_cause=missing)
    with pytest.raises(ValueError, match="Invalid minimized provider result"):
        contracts.CallResult(
            "falhou", 500, "serpro_http_500", "nao", denial_cause=TERMO
        )
    valid = contracts.CallResult("negada", 403, TERMO_020, "sim", denial_cause=TERMO)
    assert valid.denial_cause == TERMO
    for invented in ("Bearer x", "inventada"):
        with pytest.raises(ValueError, match="Invalid minimized provider result"):
            contracts.CallResult("negada", 403, TERMO_020, "sim", denial_cause=invented)


@DB
def test_a_missing_firm_termo_is_the_firms_failure_never_the_clients(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(payments, "termo_parties", lambda *_args: None)
    query = world.consult()
    assert (query.state, query.failure_code) == ("falha", "termo_ausente")
    assert effects(world)[1:3] == ("ativa", [])
    # A firm-side failure opens no client work at all.
    with tenant_context(world.termo.tenant.pk):
        assert not IntegrationIssue.objects.exists()
    assert world.sent() == []


def test_a_journal_result_omits_the_cause_and_reads_a_pre_core_receipt() -> None:
    journal = import_module("apps.integrations.journal_schema")
    old = {
        "state": "sucesso",
        "http_status": 200,
        "provider_code": "serpro_http_200",
        "billed": "sim",
        "response_digest": "a" * 64,
        "settled_microbrl": None,
    }
    result = journal._result(old)
    assert result is not None
    assert (result.provider_code, getattr(result, "denial_cause", "")) == (
        "serpro_http_200",
        "",
    )
    # A new 403 receipt is written with the same version-1 keys.
    assert hasattr(journal, "_encode")
    denial = serpro_module("adapter").classify(
        "SP-PAG", ProviderResponse(403, body_403(TERMO_020))
    )

    @dataclass(frozen=True)
    class Holder:
        result: Any

    data = json.loads(journal._encode(Holder(denial)))["result"]
    assert set(data) == set(old)
    # A pre-core 403 receipt reads back with the cause its code names.
    old_403 = {**old, "state": "negada", "http_status": 403, "provider_code": TERMO_020}
    assert journal._result(old_403).denial_cause == TERMO
    assert journal._result(data).provider_code == TERMO_020


@DB
def test_a_firm_block_raised_after_the_reservation_is_named_at_claim(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the firm issue opens between this page's reservation and its claim.
    work = import_module("apps.integrations.serpro.work")
    real = import_module("apps.integrations.call_protocol").claim

    def claim_after_block(tenant_id: Any, call_id: Any) -> Any:  # noqa: ANN401
        with tenant_context(tenant_id):
            work.own_firm_condition(world.termo.connection, work.PROC_MISSING, ["x"])
        return real(tenant_id, call_id)

    monkeypatch.setattr(payments, "claim", claim_after_block)
    try:
        query = world.consult()
    except Exception as escaped:  # noqa: BLE001 - the defect is an escaping refusal
        pytest.fail(
            f"the claim-time refusal escaped unsettled: {type(escaped).__name__}"
        )
    # Then the refusal names the firm, sends nothing and opens no client work.
    assert (query.state, query.failure_code) == (
        "falha",
        first_issue(world),
    )
    with tenant_context(world.termo.tenant.pk):
        assert not IntegrationIssue.objects.exists()
    assert world.sent() == []


@DB
def test_a_firm_block_cleared_before_the_refusal_settles_is_never_the_clients(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the firm's block refuses this page's reservation, and is resolved
    # right after, before the refusal settles (the client's 00004 is verified).
    work = import_module("apps.integrations.serpro.work")
    tenant = world.termo.tenant.pk
    with tenant_context(tenant):
        work.own_firm_condition(world.termo.connection, work.PROC_MISSING, ["x"])
    real = import_module("apps.integrations.call_protocol").reserve

    def reserve_then_resolve(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        try:
            return real(*args, **kwargs)
        finally:
            with tenant_context(tenant):
                FirmIntegrationIssue.objects.update(state="resolvida")

    monkeypatch.setattr(payments, "reserve", reserve_then_resolve)
    query = world.consult()
    # Then the refusal is retryable firm-side work, never "sem procuracao".
    # Cleared since: the recorded block (the firm issue) reads as cleared.
    assert (query.state, query.failure_code) == (
        "falha",
        f"encerrado-{first_issue(world)}",
    )
    assert effects(world)[1:3] == ("ativa", [])
    with tenant_context(tenant):
        assert not IntegrationIssue.objects.exists()
    assert "Consulte" not in history(world, query)
    assert world.sent() == []


@DB
def test_a_firm_refusal_recorded_at_dispatch_settles_by_that_cause_on_redelivery(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the firm's block opens after this page was claimed, so dispatch
    # refuses it, and the worker dies before settling that refusal.
    work = import_module("apps.integrations.serpro.work")
    tenant = world.termo.tenant.pk
    real = import_module("apps.integrations.call_protocol").claim

    def claim_then_block(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        ticket = real(*args, **kwargs)
        with tenant_context(tenant):
            work.own_firm_condition(world.termo.connection, work.PROC_MISSING, ["a"])
        return ticket

    def die(*_args: object, **_kwargs: object) -> None:
        raise SystemExit

    with monkeypatch.context() as patch:
        patch.setattr(payments, "claim", claim_then_block)
        patch.setattr(payments, "_refused", die)
        # The worker dies at settlement; a refusal escaping before it is the defect.
        with pytest.raises((SystemExit, AdmissionDeniedError)) as stopped:
            world.consult()
    assert stopped.type is SystemExit
    # Then the refusal's cause was recorded with it, in its own transaction.
    [call] = world.calls()
    assert (call.state, call.provider_code) == (
        "bloqueada_autoridade",
        first_issue(world),
    )
    # When it is redelivered once the dead runner's lease ends.
    with tenant_context(tenant):
        query = PaymentQuery.objects.get()
    lease_runs_out(world, query.pk)
    settled = payments.run_payment_query(query.tenant_id, query.pk)
    # Then it settles as the firm's, from that record, with no client work.
    assert (settled.state, settled.failure_code) == (
        "falha",
        first_issue(world),
    )
    with tenant_context(tenant):
        assert not IntegrationIssue.objects.exists()
    assert world.sent() == []


@pytest.mark.parametrize(
    "refusal",
    [
        lambda: import_module("apps.integrations.budget_pricing").BudgetDeniedError(
            "tarifa_ausente"
        ),
        lambda: AdmissionDeniedError("bloqueada_autoridade", reason="tarifa_ausente"),
    ],
    ids=["budget", "admission"],
)
def test_every_refusal_keeps_its_reason_through_the_base(refusal: Any) -> None:  # noqa: ANN401
    assert refusal().reason == "tarifa_ausente"
    # And a refusal without one keeps its state code, never a blank.
    assert AdmissionDeniedError("pausado").reason == "pausado"


@DB
def test_a_missing_tariff_refuses_with_its_own_code(
    world: World,
    settings: Any,  # noqa: ANN401
) -> None:
    settings.USAGE_UNIT_PRICES_BRL = {"serpro_emissao": "0.32"}
    query = world.consult()
    assert (query.state, query.failure_code) == ("cota_esgotada", "tarifa_ausente")
    assert world.sent() == []


@DB
def test_a_platform_403_names_the_platform_on_the_firms_next_refusal(
    world: World,
) -> None:
    # Given an accepted termo in force, and SERPRO refusing the platform's
    # contratante certificate (016) on this firm's consultation.
    platform = "AcessoNegado-ICGERENCIADOR-016"
    world.script(denied(code=platform))
    world.consult()
    # When the firm's next consultation is refused before any byte.
    refused = world.consult(nonce="consulta-2")
    assert len(world.sent()) == 1
    # Then it names the platform's 016, never a refused or missing termo.
    assert (refused.state, refused.failure_code) == (
        "falha",
        first_issue(world, platform),
    )
    detail = history(world, refused)
    assert "bloqueio do escritório" in detail
    assert platform in detail
    assert "termo" not in detail


def _revoke(world: World) -> None:
    authority = import_module("apps.integrations.authority")
    with tenant_context(world.termo.tenant.pk):
        head = head_record(world.termo.client.pk, "pagtoweb")
        assert head is not None
        authority.revoke_authority(
            world.termo.actor,
            world.termo.client.pk,
            service="pagtoweb",
            expected_version=head.version,
        )


@DB
def test_a_saved_client_refusal_settles_first_even_once_the_termo_expired(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the client's 00004 is revoked after claim, so dispatch saves the
    # client's refusal, and the worker dies before settling it.
    real = import_module("apps.integrations.call_protocol").claim

    def claim_then_revoke(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        ticket = real(*args, **kwargs)
        _revoke(world)
        return ticket

    def die(*_args: object, **_kwargs: object) -> None:
        raise SystemExit

    with monkeypatch.context() as patch:
        patch.setattr(payments, "claim", claim_then_revoke)
        patch.setattr(payments, "_refused", die)
        with pytest.raises((SystemExit, AdmissionDeniedError)) as stopped:
            world.consult()
    assert stopped.type is SystemExit
    # When it is redelivered after the firm's termo has expired.
    monkeypatch.setattr(payments, "termo_parties", lambda *_args: None)
    with tenant_context(world.termo.tenant.pk):
        query = PaymentQuery.objects.get()
    lease_runs_out(world, query.pk)
    settled = payments.run_payment_query(query.tenant_id, query.pk)
    # Then the saved refusal decides it, and its receipt and the client's work
    # are written from that record.
    assert (settled.state, settled.failure_code) == (
        "sem_procuracao",
        "autorizacao_ausente",
    )
    assert effects(world)[:3] == (["sem_procuracao"], "revogada", ["pagtoweb"])
    assert world.sent() == []


@DB
def test_a_client_refusal_at_claim_opens_the_clients_absence_issue(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given verified 00004 with no authority issue, revoked after reservation.
    real = import_module("apps.integrations.call_protocol").claim

    def revoke_then_claim(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        _revoke(world)
        return real(*args, **kwargs)

    monkeypatch.setattr(payments, "claim", revoke_then_claim)
    query = world.consult()
    # Then nothing is sent, and the client's absence is owned as its own work.
    assert (query.state, query.failure_code) == (
        "sem_procuracao",
        "autorizacao_ausente",
    )
    assert effects(world)[2] == ["pagtoweb"]
    assert world.sent() == []


def _stopped_at_settlement(
    world: World, monkeypatch: pytest.MonkeyPatch, after_claim: Callable[[], None]
) -> PaymentQuery:
    """Dispatch saves a refusal caused right after the claim; the worker then dies."""
    real = import_module("apps.integrations.call_protocol").claim

    def die(*_args: object, **_kwargs: object) -> None:
        raise SystemExit

    with monkeypatch.context() as patch:

        def claim_then_cause(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            ticket = real(*args, **kwargs)
            after_claim()
            return ticket

        patch.setattr(payments, "claim", claim_then_cause)
        patch.setattr(payments, "_refused", die)
        with pytest.raises((SystemExit, AdmissionDeniedError)) as stopped:
            world.consult()
    assert stopped.type is SystemExit
    with tenant_context(world.termo.tenant.pk):
        return PaymentQuery.objects.get(client=world.termo.client)


@DB
def test_a_client_refusal_verified_again_before_settling_opens_no_work(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the client's 00004 revoked after claim, the refusal saved, and the
    # worker dead before settling; then staff verify the procuracao again.
    query = _stopped_at_settlement(world, monkeypatch, lambda: _revoke(world))
    support.declare_and_verify(world.termo)
    lease_runs_out(world, query.pk)
    settled = payments.run_payment_query(query.tenant_id, query.pk)
    # Then the refusal stays history, and no absence work is opened for it.
    assert (settled.state, settled.failure_code) == (
        "sem_procuracao",
        "autorizacao_ausente",
    )
    assert effects(world)[1:3] == ("ativa", [])


@DB
def test_a_cleared_termo_refusal_reads_cleared_though_another_block_opened(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the firm's termo lapses after this client's claim, so dispatch saves
    # `termo_nao_vigente`, and the worker dies before settling it.
    work = import_module("apps.integrations.serpro.work")
    lapsed = {"now": False}

    def termo_in_force(*args: Any) -> bool:  # noqa: ANN401
        return not lapsed["now"] and real_termo(*args)

    real_termo = work.termo_in_force
    monkeypatch.setattr(work, "termo_in_force", termo_in_force)
    query = _stopped_at_settlement(world, monkeypatch, lambda: lapsed.update(now=True))
    [call] = world.calls()
    assert call.provider_code.startswith("termo_nao_vigente-v")
    # When the termo is in force again, and another client's 016 then opens a
    # different block (the platform's) on the same connection.
    lapsed["now"] = False
    platform = "AcessoNegado-ICGERENCIADOR-016"
    other = _second_client(world)
    world.script(denied(code=platform))
    payments.consult_payments(other, support.WINDOW, actor=None, nonce="consulta-b")
    assert effects(world)[3] != []
    # Then this client's redelivered refusal reads as its own cleared block.
    lease_runs_out(world, query.pk)
    settled = payments.run_payment_query(query.tenant_id, query.pk)
    assert (settled.state, settled.failure_code) == (
        "falha",
        f"encerrado-{call.provider_code}",
    )
    detail = history(world, settled)
    assert f"registro {call.provider_code}" in detail
    assert platform not in detail


def first_issue(world: World, code: str = "") -> str:
    """The identity a refusal records for the firm's first issue, and its 403."""
    with tenant_context(world.termo.tenant.pk):
        issue = FirmIntegrationIssue.objects.order_by("created_at", "pk").first()
    assert issue is not None
    return f"pendencia-{issue.pk}-" + (code[-3:] if code else "000")


def _second_client(world: World) -> ClientCompany:
    """Another client of the same firm connection, with a verified 00004."""
    termo = world.termo
    with tenant_context(termo.tenant.pk):
        other = ClientCompany.objects.create(
            tenant=termo.tenant, legal_name="Outro", cnpj="11444777000161", is_mei=True
        )
        ClientProviderLink.objects.create(
            tenant=termo.tenant,
            client=other,
            connection_id=termo.connection.pk,
            provider_company_ref="synthetic-pagtoweb-b",
            status="ativa",
        )
    support.declare_and_verify(replace(termo, client=other))
    return other


def _block_without_a_403(world: World) -> None:
    work = import_module("apps.integrations.serpro.work")
    with tenant_context(world.termo.tenant.pk):
        work.own_firm_condition(world.termo.connection, work.PROC_MISSING, ["sem-403"])


@DB
def test_a_resolved_issue_stays_cleared_though_a_replacement_opened(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given an issue citing no 403 refuses this page at dispatch, the worker
    # dies, and that issue is then resolved.
    query = _stopped_at_settlement(
        world, monkeypatch, lambda: _block_without_a_403(world)
    )
    first = first_issue(world)
    [call] = world.calls()
    assert call.provider_code == first
    with tenant_context(world.termo.tenant.pk):
        FirmIntegrationIssue.objects.update(state="resolvida")
    # When another client's 016 opens a replacement issue before this settles.
    other = _second_client(world)
    world.script(denied(code="AcessoNegado-ICGERENCIADOR-016"))
    payments.consult_payments(other, support.WINDOW, actor=None, nonce="consulta-b")
    with tenant_context(world.termo.tenant.pk):
        assert FirmIntegrationIssue.objects.filter(
            state__in=("aberta", "em_tratamento")
        ).exists()
    lease_runs_out(world, query.pk)
    settled = payments.run_payment_query(query.tenant_id, query.pk)
    # Then this refusal's own issue reads as resolved, not revived by the other.
    assert settled.failure_code == f"encerrado-{first}"
    detail = history(world, settled)
    assert "pendência sem código 403 registrado" in detail
    assert "aberta" not in detail


@DB
def test_a_refusals_history_keeps_the_403_frozen_when_it_was_refused(
    world: World,
) -> None:
    # Given A's 020 opens the firm issue, and the firm's next consultation is
    # refused for it: the refusal freezes that 020.
    world.script(denied(code=TERMO_020))
    world.consult()
    refused = world.consult(nonce="consulta-2")
    assert refused.failure_code == first_issue(world, TERMO_020)
    # When the issue's cited call later reads as another firm 403 (016).
    with tenant_context(world.termo.tenant.pk):
        ProviderCall.objects.filter(provider_code=TERMO_020).update(
            provider_code="AcessoNegado-ICGERENCIADOR-016"
        )
    # Then the history still names the 020 that caused it.
    detail = history(world, refused)
    assert TERMO_020 in detail
    assert "016" not in detail


@DB
def test_a_firm_termo_call_403_before_the_run_ends_it_as_the_missing_termo(
    world: World,
) -> None:
    # Given the firm's SP-TERMO call answers 403/016: that firm call (never a
    # client page) opens the issue, and the issue cites only it.
    termo, platform = world.termo, "AcessoNegado-ICGERENCIADOR-016"
    world.script(denied(code=platform))
    with tenant_context(termo.tenant.pk):
        version = ProviderConnection.objects.get(pk=termo.connection.pk).version
    submit_termo(
        termo.actor,
        termo.tenant.pk,
        TermoSubmission(
            termo.connection.pk,
            termo.document.pk,
            version,
            termo.parties,
            world.gateway.origin,
        ),
    )
    # When this client's consultation runs: the refused termo is the head, so
    # the firm has no termo in force and no page is ever reserved.
    refused = world.consult()
    assert world.sent() == []
    # Then it ends as the firm's missing termo, stated as such, never the client's.
    assert (refused.state, refused.failure_code) == ("falha", "termo_ausente")
    detail = history(world, refused)
    assert "não havia termo de autorização do escritório em vigor" in detail
    assert "procuração do cliente não foi alterada" in detail


@DB
def test_a_page_2_block_after_a_billed_page_1_states_both_pages(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given page 1 is full (so page 2 follows), sent and billed; then a firm
    # issue opens before page 2 is reserved.
    real = import_module("apps.integrations.call_protocol").reserve
    reserved = []

    def reserve_then_block(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        reserved.append(1)
        if len(reserved) == 2:
            _block_without_a_403(world)
        return real(*args, **kwargs)

    monkeypatch.setattr(payments, "reserve", reserve_then_block)
    world.script(page(support.darfs(100)))
    refused = world.consult()
    # Then history states page 1 as sent and billed, and page 2 as refused.
    assert (refused.state, refused.pages_fetched, len(world.sent())) == (
        "falha",
        1,
        1,
    )
    detail = history(world, refused)
    assert "A admissão recusou a consulta por um bloqueio do escritório" in detail
    assert "antes do envio" not in detail


@DB
def test_a_termo_gone_on_redelivery_after_a_billed_page_states_that_page(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given page 1 full, sent, billed and committed; the worker stops before it
    # reserves page 2, and the firm's termo is gone before the redelivery.
    real = import_module("apps.integrations.call_protocol").reserve
    reserved = []

    def stop_before_page_2(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        reserved.append(1)
        if len(reserved) == 2:
            raise SystemExit
        return real(*args, **kwargs)

    world.script(page(support.darfs(100)))
    with monkeypatch.context() as patch:
        patch.setattr(payments, "reserve", stop_before_page_2)
        with pytest.raises(SystemExit):
            world.consult()
    with tenant_context(world.termo.tenant.pk):
        query = PaymentQuery.objects.get(client=world.termo.client)
    monkeypatch.setattr(payments, "termo_parties", lambda *_args: None)
    lease_runs_out(world, query.pk)
    settled = payments.run_payment_query(query.tenant_id, query.pk)
    # Then it ends as the missing termo, and history states the billed page 1.
    assert (settled.state, settled.failure_code, settled.pages_fetched) == (
        "falha",
        "termo_ausente",
        1,
    )
    detail = history(world, settled)
    assert "A consulta parou: não havia termo" in detail


@DB
def test_the_block_an_sp_termo_403_opens_freezes_that_firm_calls_code(
    world: World,
) -> None:
    # Given the firm's SP-TERMO call answers 403/016: the issue it opens cites
    # only that firm call, never a client page.
    termo, platform = world.termo, "AcessoNegado-ICGERENCIADOR-016"
    world.script(denied(code=platform))
    with tenant_context(termo.tenant.pk):
        version = ProviderConnection.objects.get(pk=termo.connection.pk).version
    submit_termo(
        termo.actor,
        termo.tenant.pk,
        TermoSubmission(
            termo.connection.pk,
            termo.document.pk,
            version,
            termo.parties,
            world.gateway.origin,
        ),
    )
    # Then the block a refusal would record now freezes that call's 016.
    work = import_module("apps.integrations.serpro.work")
    with tenant_context(termo.tenant.pk):
        connection = ProviderConnection.objects.get(pk=termo.connection.pk)
        assert work.firm_block(connection) == first_issue(world, platform)


@DB
def test_a_partial_quota_after_a_billed_page_ends_by_the_budget_refusal(
    world: World,
) -> None:
    # Given the budget covers one page, and page 1 is full (so page 2 follows).
    with tenant_context(world.termo.tenant.pk):
        ProviderBudget.objects.update(cap_microbrl=300_000)
    world.script(page(support.darfs(100)))
    stopped = world.consult()
    # Then the quota stops page 2 before sending, after page 1 was billed.
    assert (stopped.state, stopped.failure_code, stopped.pages_fetched) == (
        "falha",
        "cota_esgotada",
        1,
    )
    assert len(world.sent()) == 1
    assert history(world, stopped) == (
        "O controle de custos recusou a página 2 antes do envio: o limite mensal "
        "de consultas acabou. A página 1 foi enviada e cobrada."
    )


@DB
def test_a_partial_quota_records_the_budgets_own_reason(
    world: World, monkeypatch: pytest.MonkeyPatch, settings: SettingsWrapper
) -> None:
    # Given page 1 full and billed; then the consulta tariff is gone before
    # page 2 is reserved, while quota is still available.
    real = import_module("apps.integrations.call_protocol").reserve
    reserved = []

    def reserve_without_tariff(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        reserved.append(1)
        if len(reserved) == 2:
            settings.USAGE_UNIT_PRICES_BRL = {"serpro_emissao": "0.32"}
        return real(*args, **kwargs)

    monkeypatch.setattr(payments, "reserve", reserve_without_tariff)
    world.script(page(support.darfs(100)))
    stopped = world.consult()
    # Then the refusal keeps the budget's own reason, never a generic quota one.
    assert (stopped.state, stopped.failure_code, stopped.pages_fetched) == (
        "falha",
        "tarifa_ausente",
        1,
    )
    assert len(world.sent()) == 1


@DB
def test_a_quota_refusal_at_claim_records_the_budgets_own_reason(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given page 1 full and billed; page 2 is reserved, then the budget window
    # turns over before its claim, so the claim refuses the held reservation.
    real = import_module("apps.integrations.call_protocol").reserve
    reserved = []

    def reserve_then_turn_the_window(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        row = real(*args, **kwargs)
        reserved.append(row)
        if len(reserved) == 2:
            with tenant_context(world.termo.tenant.pk):
                budget = ProviderBudget.objects.get()
                earlier = (budget.window_start - timedelta(days=1)).replace(day=1)
                ProviderBudget.objects.update(window_start=earlier)
        return row

    monkeypatch.setattr(payments, "reserve", reserve_then_turn_the_window)
    world.script(page(support.darfs(100)))
    stopped = world.consult()
    # Then page 2's call records the claim's quota refusal, and the consultation
    # keeps that same reason (AXRM-045).
    assert [c.state for c in world.calls()] == ["sucesso", "bloqueada_cota"]
    assert (stopped.state, stopped.failure_code, stopped.pages_fetched) == (
        "falha",
        "reserva_vencida",
        1,
    )
    assert len(world.sent()) == 1


@DB
def test_a_saved_quota_refusal_wins_over_a_gone_termo_on_redelivery(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given page 1 full, sent and billed; page 2 is reserved, then the budget
    # window turns over, so its claim commits a quota refusal on the call...
    real = import_module("apps.integrations.call_protocol").reserve
    reserved = []

    def reserve_then_turn_the_window(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        row = real(*args, **kwargs)
        reserved.append(row)
        if len(reserved) == 2:
            with tenant_context(world.termo.tenant.pk):
                budget = ProviderBudget.objects.get()
                earlier = (budget.window_start - timedelta(days=1)).replace(day=1)
                ProviderBudget.objects.update(window_start=earlier)
        return row

    def die(*_args: object, **_kwargs: object) -> None:
        raise SystemExit

    world.script(page(support.darfs(100)))
    with monkeypatch.context() as patch:
        patch.setattr(payments, "reserve", reserve_then_turn_the_window)
        # ...and the worker stops before settling it.
        patch.setattr(payments, "_refused_quota", die)
        with pytest.raises(SystemExit):
            world.consult()
    assert [c.state for c in world.calls()] == ["sucesso", "bloqueada_cota"]
    with tenant_context(world.termo.tenant.pk):
        query = PaymentQuery.objects.get(client=world.termo.client)
    # When the firm's termo is gone and the task is redelivered.
    monkeypatch.setattr(payments, "termo_parties", lambda *_args: None)
    lease_runs_out(world, query.pk)
    settled = payments.run_payment_query(query.tenant_id, query.pk)
    # Then the refusal already recorded on the call wins over the live preflight,
    # and its quota receipt is written after page 1's (AXRM-045).
    assert (settled.state, settled.failure_code, settled.pages_fetched) == (
        "falha",
        "reserva_vencida",
        1,
    )
    assert receipts(world) == [("encontrado", 100), ("cota_esgotada", 0)]
    assert len(world.sent()) == 1


@DB
def test_a_firm_termo_call_403_after_the_claim_is_frozen_in_the_refusal(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given this page claimed, then the firm's SP-TERMO call answers 403/016
    # before the page dispatches (the connection version moves): that firm call
    # opens the issue, and admission names it at dispatch, never a bare refusal.
    termo, platform = world.termo, "AcessoNegado-ICGERENCIADOR-016"
    real = import_module("apps.integrations.call_protocol").claim

    def claim_then_termo_403(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        ticket = real(*args, **kwargs)
        world.script(denied(code=platform))
        with tenant_context(termo.tenant.pk):
            version = ProviderConnection.objects.get(pk=termo.connection.pk).version
        submit_termo(
            termo.actor,
            termo.tenant.pk,
            TermoSubmission(
                termo.connection.pk,
                termo.document.pk,
                version,
                termo.parties,
                world.gateway.origin,
            ),
        )
        return ticket

    monkeypatch.setattr(payments, "claim", claim_then_termo_403)
    refused = world.consult()
    assert world.sent() == []
    # Then the refusal froze the 016 that firm call recorded, and history names it.
    assert (refused.state, refused.failure_code) == (
        "falha",
        first_issue(world, platform),
    )
    assert platform in history(world, refused)


@DB
def test_a_receipt_whose_connection_moved_before_claim_is_refused_and_released(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given one DAS payment found by a successful consultation.
    world.script(page([support.item("DAS000000000045")]))
    world.consult()
    with tenant_context(world.termo.tenant.pk):
        das = FederalPayment.objects.get()
    # When its SP-COMP is reserved at v1, then a termo renewal moves the
    # connection to v2 before the claim.
    real = import_module("apps.integrations.call_protocol").reserve

    def reserve_then_renew(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        row = real(*args, **kwargs)
        with tenant_context(world.termo.tenant.pk):
            ProviderConnection.objects.filter(pk=world.termo.connection.pk).update(
                version=F("version") + 1
            )
        return row

    monkeypatch.setattr(payments, "reserve", reserve_then_renew)
    tenant, actor = world.termo.tenant.pk, world.termo.actor
    try:
        call = payments.emit_payment_receipt(tenant, das.pk, actor.pk)
    except AdmissionDeniedError:
        pytest.fail("the claim's refusal escaped the receipt worker")
    # Then the call is a recorded refusal with its reservation released.
    # The returned call is the stored one: never the row as it was reserved.
    assert call is not None
    assert call.state == "bloqueada_autoridade"
    with tenant_context(tenant):
        held = ProviderReservation.objects.filter(call=call, state="reserved")
        assert not held.exists()
    assert world.sent("/Emitir") == []


@DB
def test_a_receipt_retry_after_a_401_refused_at_claim_is_reported_as_stored(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given one DAS payment found by a successful consultation.
    world.script(page([support.item("DAS000000000045")]))
    world.consult()
    with tenant_context(world.termo.tenant.pk):
        das = FederalPayment.objects.get()
    # When its first SP-COMP attempt gets a complete 401, and the connection
    # version moves before the retry's claim.
    protocol = import_module("apps.integrations.call_protocol")
    real_claim = protocol.claim

    def renew_then_claim(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        with tenant_context(world.termo.tenant.pk):
            ProviderConnection.objects.filter(pk=world.termo.connection.pk).update(
                version=F("version") + 1
            )
        return real_claim(*args, **kwargs)

    monkeypatch.setattr(protocol, "claim", renew_then_claim)
    real_perform = protocol.perform
    escaped: list[AdmissionDeniedError] = []

    def observed_perform(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        try:
            return real_perform(*args, **kwargs)
        except AdmissionDeniedError as error:
            escaped.append(error)
            raise

    monkeypatch.setattr(payments, "perform", observed_perform)
    world.script(Reply(status=401))
    tenant, actor = world.termo.tenant.pk, world.termo.actor
    try:
        call = payments.emit_payment_receipt(tenant, das.pk, actor.pk)
    except AdmissionDeniedError:
        pytest.fail("the retry claim's refusal escaped the receipt worker")
    # perform itself returns once the retry claim's refusal is recorded; it never
    # hands that refusal to its caller (AXRM-045).
    assert escaped == [], "perform raised the retry claim's refusal"
    # Then the retry is a recorded refusal, reported as stored, released.
    assert call is not None
    assert call.state == "bloqueada_autoridade"
    with tenant_context(tenant):
        held = ProviderReservation.objects.filter(call=call, state="reserved")
        assert not held.exists()
    assert len(world.sent("/Emitir")) == 1


def _renew(world: World) -> None:
    with tenant_context(world.termo.tenant.pk):
        ProviderConnection.objects.filter(pk=world.termo.connection.pk).update(
            version=F("version") + 1
        )


@DB
def test_a_receipt_refused_at_its_first_dispatch_is_reported_as_stored(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given one DAS payment found by a successful consultation.
    world.script(page([support.item("DAS000000000045")]))
    world.consult()
    with tenant_context(world.termo.tenant.pk):
        das = FederalPayment.objects.get()
    # When its SP-COMP is claimed at v1, then a termo renewal moves the
    # connection to v2 before the dispatch.
    real_claim = import_module("apps.integrations.call_protocol").claim

    def claim_then_renew(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        ticket = real_claim(*args, **kwargs)
        _renew(world)
        return ticket

    monkeypatch.setattr(payments, "claim", claim_then_renew)
    tenant, actor = world.termo.tenant.pk, world.termo.actor
    try:
        call = payments.emit_payment_receipt(tenant, das.pk, actor.pk)
    except AdmissionDeniedError:
        call = None
    # Then the dispatch refusal is recorded, released and reported as stored.
    assert call is not None, "the dispatch refusal escaped the receipt worker"
    assert call.state == "bloqueada_autoridade"
    with tenant_context(tenant):
        held = ProviderReservation.objects.filter(call=call, state="reserved")
        assert not held.exists()
    assert world.sent("/Emitir") == []


@DB
def test_a_receipt_refused_at_dispatch_after_a_401_retry_claim_is_stored(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given one DAS payment found by a successful consultation.
    world.script(page([support.item("DAS000000000045")]))
    world.consult()
    with tenant_context(world.termo.tenant.pk):
        das = FederalPayment.objects.get()
    # When its first SP-COMP attempt gets a complete 401, the retry's claim
    # succeeds, and the connection version moves before the retry's dispatch.
    protocol = import_module("apps.integrations.call_protocol")
    real_claim = protocol.claim

    def claim_then_renew(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        ticket = real_claim(*args, **kwargs)
        _renew(world)
        return ticket

    monkeypatch.setattr(protocol, "claim", claim_then_renew)
    world.script(Reply(status=401))
    tenant, actor = world.termo.tenant.pk, world.termo.actor
    try:
        call = payments.emit_payment_receipt(tenant, das.pk, actor.pk)
    except AdmissionDeniedError:
        call = None
    # Then the retry's dispatch refusal is recorded, released, reported as stored.
    assert call is not None, "the retry dispatch refusal escaped the receipt worker"
    assert call.state == "bloqueada_autoridade"
    with tenant_context(tenant):
        held = ProviderReservation.objects.filter(call=call, state="reserved")
        assert not held.exists()
    assert len(world.sent("/Emitir")) == 1


@DB
def test_a_first_page_204_is_stated_as_sent_without_a_charge(world: World) -> None:
    # Given SERPRO answers the first page with a 204: an empty, unbilled page.
    world.script(Reply(status=204, body=b""))
    query = world.consult()
    assert query.state == "nada_localizado"
    # Then its history never claims a charge the call does not record (AXRM-045).
    assert history(world, query) == (
        "Nenhum pagamento localizado no intervalo consultado. Isso não indica "
        "falta de pagamento. A página 1 foi enviada, sem cobrança."
    )


@DB
def test_a_missing_tariff_is_stated_by_its_own_reason_not_as_a_spent_quota(
    world: World,
    settings: Any,  # noqa: ANN401
) -> None:
    # Given quota is available but no tariff is configured for this consultation.
    settings.USAGE_UNIT_PRICES_BRL = {"serpro_emissao": "0.32"}
    query = world.consult()
    assert (query.state, query.failure_code) == ("cota_esgotada", "tarifa_ausente")
    # Then the history names the missing tariff, never an exhausted cap.
    text = history(world, query)
    assert "não há tarifa cadastrada para esta consulta" in text
    assert "limite mensal" not in text
    assert text.endswith("Nenhuma página foi enviada.")


@DB
def test_a_billed_unsettled_page_stays_sent_and_billed_after_the_termo_goes(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given page 1 gets a malformed paid 200, finalized as billed, and the worker
    # stops before settling it...
    world.script(Reply(body=b'{"status":200,"mensagens":[],"dados":"{}"}'))

    def die(*_args: object, **_kwargs: object) -> None:
        raise SystemExit

    with monkeypatch.context() as patch:
        patch.setattr(payments, "_settle_page", die)
        with pytest.raises(SystemExit):
            world.consult()
    [call] = world.calls()
    assert (call.state, call.billed) == ("falhou", "sim")
    with tenant_context(world.termo.tenant.pk):
        query = PaymentQuery.objects.get(client=world.termo.client)
    # ...when the termo is gone and the task is redelivered.
    monkeypatch.setattr(payments, "termo_parties", lambda *_args: None)
    lease_runs_out(world, query.pk)
    settled = payments.run_payment_query(query.tenant_id, query.pk)
    assert (settled.state, settled.failure_code, settled.pages_fetched) == (
        "falha",
        "termo_ausente",
        0,
    )
    # Then the history keeps the recorded send and charge (AXRM-045).
    assert history(world, settled).endswith("A página 1 foi enviada e cobrada.")


@pytest.mark.parametrize(
    ("state", "code", "why"),
    [
        (
            "cota_esgotada",
            "cota_esgotada",
            ": o limite mensal de consultas acabou.",
        ),
        (
            "cota_esgotada",
            "cota_ausente",
            ": não há limite mensal de consultas configurado.",
        ),
        (
            "cota_esgotada",
            "tarifa_ausente",
            ": não há tarifa cadastrada para esta consulta.",
        ),
        (
            "cota_esgotada",
            "tarifa_invalida",
            ": a tarifa cadastrada para esta consulta é inválida.",
        ),
        (
            "falha",
            "reserva_ausente",
            ": a reserva de custo da página não foi encontrada.",
        ),
        (
            "falha",
            "reserva_vencida",
            ": a reserva de custo da página venceu antes do envio.",
        ),
        ("falha", "cota_esgotada_parcial", " (registro cota_esgotada_parcial)."),
        ("cota_esgotada", "orcamento_novo", " (registro orcamento_novo)."),
    ],
    ids=lambda value: value if isinstance(value, str) and " " not in value else None,
)
def test_every_budget_reason_has_its_own_sentence(
    state: str, code: str, why: str
) -> None:
    query = PaymentQuery(state=state, failure_code=code, pages_fetched=0)
    assert query_detail(query, []) == (
        f"O controle de custos recusou a consulta antes do envio{why} "
        "Nenhuma página foi enviada."
    )


@pytest.mark.parametrize(
    ("pages", "facts"),
    [
        ([], "Nenhuma página foi enviada."),
        ([(1, False, "desconhecido")], "Nenhuma página foi enviada."),
        ([(1, True, "sim")], "A página 1 foi enviada e cobrada."),
        ([(1, True, "nao")], "A página 1 foi enviada, sem cobrança."),
        (
            [(1, True, "sim"), (2, True, "sim")],
            "As páginas 1 e 2 foram enviadas e cobradas.",
        ),
        (
            [(1, True, "sim"), (2, True, "sim"), (3, True, "sim")],
            "As páginas 1 a 3 foram enviadas e cobradas.",
        ),
        (
            [(1, True, "sim"), (2, True, "nao")],
            (
                "As páginas 1 e 2 foram enviadas: a página 1 foi cobrada; "
                "a página 2 sem cobrança."
            ),
        ),
        (
            [(1, True, "sim"), (2, True, "desconhecido"), (3, False, "desconhecido")],
            (
                "As páginas 1 e 2 foram enviadas: a página 1 foi cobrada; "
                "a cobrança não é conhecida para a página 2."
            ),
        ),
    ],
    ids=["none", "reserved", "billed", "unbilled", "pair", "range", "mixed", "unknown"],
)
def test_page_facts_come_from_each_recorded_page_call(
    pages: list[tuple[int, bool, str]], facts: str
) -> None:
    panel_module = import_module("apps.integrations.serpro.payments_panel")
    recorded = [
        panel_module.PageFact(number, sent, billed, refused=False)
        for number, sent, billed in pages
    ]
    assert panel_module.page_facts(recorded) == facts


@DB
def test_a_budget_error_while_settling_a_sent_receipt_is_not_reported_as_stored(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given one DAS payment found by a successful consultation.
    world.script(page([support.item("DAS000000000045")]))
    world.consult()
    with tenant_context(world.termo.tenant.pk):
        das = FederalPayment.objects.get()
    # When the SP-COMP is sent, and while it is on the wire its reservation and
    # budget month stop matching, so settling it raises a budget refusal.
    protocol = import_module("apps.integrations.call_protocol")
    real_transmit = protocol.transmit

    def transmit_then_lose_the_budget(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        reply = real_transmit(*args, **kwargs)
        with tenant_context(world.termo.tenant.pk):
            ProviderReservation.objects.filter(call__operation="SP-COMP").update(
                attempt_number=F("attempt_number") + 50
            )
            budget = ProviderBudget.objects.get()
            earlier = (budget.window_start - timedelta(days=1)).replace(day=1)
            ProviderBudget.objects.update(window_start=earlier)
        return reply

    monkeypatch.setattr(protocol, "transmit", transmit_then_lose_the_budget)
    tenant, actor = world.termo.tenant.pk, world.termo.actor
    escaped: list[AdmissionDeniedError] = []
    try:
        payments.emit_payment_receipt(tenant, das.pk, actor.pk)
    except AdmissionDeniedError as error:
        escaped.append(error)
    # Then that unrecorded refusal reaches the task, never a "stored" call.
    assert [error.reason for error in escaped] == ["cota_ausente"], (
        "the receipt worker swallowed a refusal it never recorded"
    )
    with tenant_context(tenant):
        call = ProviderCall.objects.get(operation="SP-COMP")
    assert call.state not in ("bloqueada_autoridade", "pausada", "bloqueada_cota")


@DB
def test_a_missing_tariff_never_wears_the_limit_reached_badge(
    world: World,
    settings: Any,  # noqa: ANN401
) -> None:
    settings.USAGE_UNIT_PRICES_BRL = {"serpro_emissao": "0.32"}
    query = world.consult()
    assert (query.state, query.failure_code) == ("cota_esgotada", "tarifa_ausente")
    # Then the badge names the cost control, never an exhausted limit (AXRM-045).
    assert history_row(world, query).label == "Recusada pelo controle de custos"


@DB
@pytest.mark.parametrize(
    ("platform_cap", "code"),
    [({"serpro": "0"}, "cota_esgotada"), ({}, "cota_ausente")],
    ids=["platform-spent", "platform-absent"],
)
def test_a_platform_cap_refusal_never_blames_the_firms_limit(
    world: World,
    settings: Any,  # noqa: ANN401
    platform_cap: dict[str, str],
    code: str,
) -> None:
    # Given the firm's own budget has room, and only the platform's cap refuses.
    settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL = platform_cap
    query = world.consult()
    assert (query.state, query.failure_code) == ("cota_esgotada", code)
    # Then the history names the cap without claiming it was the firm's.
    text = history(world, query)
    assert "escritório" not in text
    assert "limite mensal de consultas" in text


@DB
def test_a_401_first_send_stays_sent_when_its_retry_is_refused_at_dispatch(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given page 1 gets a complete 401, its retry is claimed, and the budget
    # month turns over before that retry is dispatched.
    protocol = import_module("apps.integrations.call_protocol")
    real_claim = protocol.claim

    def claim_then_turn_the_window(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        ticket = real_claim(*args, **kwargs)
        with tenant_context(world.termo.tenant.pk):
            budget = ProviderBudget.objects.get()
            earlier = (budget.window_start - timedelta(days=1)).replace(day=1)
            ProviderBudget.objects.update(window_start=earlier)
        return ticket

    monkeypatch.setattr(protocol, "claim", claim_then_turn_the_window)
    world.script(Reply(status=401))
    query = world.consult()
    assert (query.state, query.failure_code) == ("cota_esgotada", "reserva_vencida")
    assert len(world.sent()) == 1
    # Then the 401 send stays in the history, and the refusal is its retry's.
    assert history(world, query) == (
        "O controle de custos recusou a nova tentativa da página 1 antes do envio: "
        "a reserva de custo da página venceu antes do envio. "
        "A página 1 foi enviada, sem cobrança."
    )


@DB
def test_an_unknown_budget_code_on_a_later_page_names_that_page(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given page 1 is full and billed, and the budget refuses page 2's
    # reservation with a code this release does not know.
    budget_module = import_module("apps.integrations.budget")
    real_check = budget_module.check_cap
    priced: list[int] = []

    def refuse_the_second_page(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        if args[1] > 0:
            priced.append(args[1])
            if len(priced) == 2:
                unknown = "orcamento_novo"
                pricing = import_module("apps.integrations.budget_pricing")
                raise pricing.BudgetDeniedError(unknown)
        real_check(*args, **kwargs)

    monkeypatch.setattr(budget_module, "check_cap", refuse_the_second_page)
    world.script(page(support.darfs(100)))
    query = world.consult()
    assert (query.state, query.failure_code) == ("falha", "orcamento_novo")
    # Then the generic budget sentence still names page 2 and the code.
    assert history(world, query) == (
        "O controle de custos recusou a página 2 antes do envio "
        "(registro orcamento_novo). A página 1 foi enviada e cobrada."
    )
