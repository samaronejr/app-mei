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
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.integrations.authority import head_record
from apps.integrations.call_contracts import AdmissionDeniedError
from apps.integrations.models import (
    ClientProviderLink,
    IntegrationIssue,
)
from apps.integrations.serpro import payments
from apps.integrations.serpro.models import FirmIntegrationIssue, PaymentQuery
from apps.integrations.serpro.payments_panel import federal_payments_panel as panel
from apps.integrations.serpro.payments_panel import query_detail
from apps.integrations.transport_types import ProviderResponse
from apps.obligations.models import FederalConsultation, FederalPayment
from tests.integrations import pagtoweb_support as support
from tests.integrations.pagtoweb_support import World, denied, page, world
from tests.integrations.serpro_support import serpro_module
from tests.integrations.test_pagtoweb_ownership import lease_runs_out

__all__ = ["world"]
# Only the flows touch the database; the table and code checks are pure.
DB: Final = pytest.mark.django_db(transaction=True)
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
    detail = query_detail(query)
    assert ("Sem procuração" in detail) is (cause == CLIENT)
    if cause != CLIENT:
        expected = denials_api().DENIAL_TEXT[cause]
        assert detail == f"{expected} (código {code}). Consulta não repetida."
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
        f"bloqueio-{TERMO_020}",
    )
    detail = query_detail(refused)
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
    assert U99 in query_detail(query)
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
        ("falha", "limite_de_paginas", "A consulta atingiu o limite de 10 páginas"),
        ("falha", "resposta_pagtoweb_invalida", "A consulta não foi concluída"),
        ("incerto", "pagina_nao_registrada", "O resultado é incerto. Confira"),
        ("falha", "termo_ausente", "O termo de autorização do escritório"),
    ],
    ids=["page-limit", "failure", "uncertain", "firm-termo"],
)
def test_a_failure_that_is_not_a_403_keeps_its_own_copy(
    state: str, code: str, detail: str
) -> None:
    query = PaymentQuery(state=state, failure_code=code, pages_fetched=10)
    assert query_detail(query).startswith(detail)


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
        "autorizacao_procurador_ausente",
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
        "encerrado-autorizacao_procurador_ausente",
    )
    assert effects(world)[1:3] == ("ativa", [])
    with tenant_context(tenant):
        assert not IntegrationIssue.objects.exists()
    assert "Consulte novamente" in query_detail(query)
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
        "autorizacao_procurador_ausente",
    )
    # When it is redelivered once the dead runner's lease ends.
    with tenant_context(tenant):
        query = PaymentQuery.objects.get()
    lease_runs_out(world, query.pk)
    settled = payments.run_payment_query(query.tenant_id, query.pk)
    # Then it settles as the firm's, from that record, with no client work.
    assert (settled.state, settled.failure_code) == (
        "falha",
        "autorizacao_procurador_ausente",
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
    assert (refused.state, refused.failure_code) == ("falha", f"bloqueio-{platform}")
    detail = query_detail(refused)
    assert "credenciais da plataforma" in detail
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
    assert call.provider_code == "termo_nao_vigente"
    # When the termo is in force again, and another client's 016 then opens a
    # different block (the platform's) on the same connection.
    lapsed["now"] = False
    termo, platform = world.termo, "AcessoNegado-ICGERENCIADOR-016"
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
    world.script(denied(code=platform))
    payments.consult_payments(other, support.WINDOW, actor=None, nonce="consulta-b")
    assert effects(world)[3] != []
    # Then this client's redelivered refusal reads as its own cleared block.
    lease_runs_out(world, query.pk)
    settled = payments.run_payment_query(query.tenant_id, query.pk)
    assert (settled.state, settled.failure_code) == (
        "falha",
        "encerrado-termo_nao_vigente",
    )
    detail = query_detail(settled)
    assert "já está em vigor" in detail
    assert platform not in detail
