"""AXRM-045: a SERPRO 403 is classified by its documented reason code (A6, RS-2).

Table-driven over every HTTP 403 code of the official message table (fixture read
2026-11-22): cause, owner, effect on the client's authority, billing and copy.
Only the client-procuracao group demotes an authority; a firm-termo or platform
code opens the firm issue, which refuses that firm's next calls; a provider
failure demotes nothing; an unknown code is `incerto`. No 403 is ever re-sent.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from types import ModuleType
from typing import Any, Final
from uuid import UUID

import pytest
from django.db.models import F
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.integrations.authority import head_record, revoke_authority
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.call_contracts import AdmissionDeniedError
from apps.integrations.call_models import CallState, ProviderCall, ProviderCallAttempt
from apps.integrations.models import (
    ClientProviderLink,
    IntegrationIssue,
    ProviderConnection,
)
from apps.integrations.serpro import payments
from apps.integrations.serpro.authorization import TermoSubmission, submit_termo
from apps.integrations.serpro.codec import build_request
from apps.integrations.serpro.models import FirmIntegrationIssue, PaymentQuery
from apps.integrations.serpro.parties import termo_parties
from apps.integrations.serpro.payments_panel import federal_payments_panel as panel
from apps.integrations.serpro.payments_panel import query_detail
from apps.integrations.serpro.reauthentication import COMPLETE_401
from apps.integrations.serpro.work import ADMISSION_REFUSED, CLIENT_ABSENT, TERMO_PREFIX
from apps.integrations.transport_types import ProviderResponse
from apps.obligations.models import FederalConsultation, FederalPayment
from apps.obligations.models.federal import FederalConsultationKind
from tests.fakes.provider import Reply
from tests.integrations import pagtoweb_support as support
from tests.integrations.pagtoweb_support import World, denied, page, world
from tests.integrations.serpro_support import serpro_module
from tests.integrations.test_pagtoweb import receipts
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
    assert ("falta da procuração" in detail) is (cause == CLIENT)
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
        first_issue(world, TERMO_020),
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
    assert "Consulte" not in query_detail(query)
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
    detail = query_detail(refused)
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
    detail = query_detail(settled)
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
    detail = query_detail(settled)
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
    detail = query_detail(refused)
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
    detail = query_detail(refused)
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
    detail = query_detail(refused)
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
    detail = query_detail(settled)
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
    assert (
        query_detail(stopped)
        == "A consulta não foi concluída (registro cota_esgotada)."
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
    assert platform in query_detail(refused)


# The definitive 401 a retry claim follows (`serpro/reauthentication.py:27-50`).
DEFINITIVE_401: Final = {
    "attempt_number": 1,
    "observation": "result",
    "outcome": CallState.FAILED,
    "http_status": 401,
    "provider_code": COMPLETE_401,
    "billed": "nao",
    "settled_microbrl": 0,
}
# The receipt's priced emission and the page's priced consultation.
RECEIPT_MICROBRL: Final = 320_000
# A budget row's identity: compared at every checkpoint, never its version.
BUDGET_KEY: Final = ("id", "tenant_id", "provider", "window_start")
# The call's lease and fencing fields, which only a successful claim advances.
LEASE: Final = ("fence", "lease_owner", "lease_until", "execution_epoch", "version")


def _sha(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _pin_budget(world: World) -> tuple[object, ...]:
    """The one monthly budget's identity, captured right after the seed."""
    with tenant_context(world.termo.tenant.pk):
        [row] = ProviderBudget.objects.values(*BUDGET_KEY)
    return tuple(row[key] for key in BUDGET_KEY)


def _target(world: World, call_id: UUID) -> dict[str, Any]:
    """The bound call, its observations and slots, and every budget: full rows."""
    with tenant_context(world.termo.tenant.pk):
        return {
            "call": ProviderCall.objects.filter(pk=call_id).values().get(),
            "observations": list(
                ProviderCallAttempt.objects.filter(call_id=call_id)
                .order_by("attempt_number", "observation")
                .values()
            ),
            "slots": list(
                ProviderReservation.objects.filter(call_id=call_id)
                .order_by("attempt_number")
                .values()
            ),
            "budgets": list(ProviderBudget.objects.order_by("pk").values()),
        }


def _budgets(world: World) -> dict[str, Any]:
    """Every budget row, with no call yet: the post-seed baseline."""
    with tenant_context(world.termo.tenant.pk):
        return {
            "slots": [],
            "budgets": list(ProviderBudget.objects.order_by("pk").values()),
        }


def _on_budget(snapshot: dict[str, Any], pinned: tuple[object, ...]) -> tuple[int, ...]:
    """Require the pinned budget alone, every slot drawn on it; its balances."""
    [budget] = snapshot["budgets"]
    assert tuple(budget[key] for key in BUDGET_KEY) == pinned, "another budget row"
    assert [slot["budget_id"] for slot in snapshot["slots"]] == [pinned[0]] * len(
        snapshot["slots"]
    ), "a slot is not drawn on the pinned budget"
    return budget["reserved"], budget["known_liability"], budget["uncertain_exposure"]


def _slots(snapshot: dict[str, Any]) -> list[tuple[int, str, int]]:
    return [
        (slot["attempt_number"], slot["state"], slot["microbrl"])
        for slot in snapshot["slots"]
    ]


def _delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Every field of one row that differs between two snapshots of it."""
    return {
        name: (before[name], after[name])
        for name in sorted(set(before) | set(after))
        if before.get(name) != after.get(name)
    }


def _recording_claim(
    world: World,
    module: ModuleType,
    refusals: list[tuple[UUID, str, str, str, dict[str, Any]]],
    entry: Callable[[UUID], None],
) -> Callable[[UUID, UUID], Any]:
    """The real claim at this site, after `entry`; each refusal kept at its exit."""
    real = module.claim

    def claim(tenant_id: UUID, call_id: UUID) -> Any:  # noqa: ANN401
        entry(call_id)
        try:
            return real(tenant_id, call_id)
        except AdmissionDeniedError as error:
            # Named by string: the typed class is absent on the red trees.
            refusals.append(
                (
                    call_id,
                    type(error).__name__,
                    str(error),
                    error.reason,
                    _target(world, call_id),
                )
            )
            raise

    return claim


def _tag(request: Any) -> UUID | None:  # noqa: ANN401 - the fake's received request
    value = next((v for k, v in request.headers if k.lower() == "x-request-tag"), None)
    return UUID(value) if value else None


def _observe_perform(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Record what the caller's own perform does: return its result, or raise."""
    real = import_module("apps.integrations.call_protocol").perform
    seen: list[tuple[Any, ...]] = []

    def observed(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        try:
            result = real(*args, **kwargs)
        except AdmissionDeniedError as error:
            seen.append(("raised", type(error).__name__, error.reason))
            raise
        seen.append(("returned", result))
        return result

    monkeypatch.setattr(payments, "perform", observed)
    return seen


def _the_401(result: Any) -> tuple[object, ...]:  # noqa: ANN401 - a CallResult
    return (
        result.state,
        result.http_status,
        result.provider_code,
        result.billed,
        result.settled_microbrl,
        result.response_digest,
        result.denial_cause,
    )


def _found_payment(world: World) -> FederalPayment:
    """One DAS payment found by a successful consultation (the seed page)."""
    world.script(page([support.item("DAS000000000045")]))
    world.consult()
    with tenant_context(world.termo.tenant.pk):
        return FederalPayment.objects.get()


def _payment_key(das: FederalPayment) -> str:
    """The receipt's one intent, derived from the input payment alone."""
    key = _sha("pagtoweb-comprovante", str(das.pk))
    assert payments.receipt_request_key(das.pk) == key
    return key


def _intent(snapshot: dict[str, Any]) -> tuple[str, str]:
    return snapshot["call"]["request_key"], snapshot["call"]["business_effect_key"]


@DB
def test_a_receipt_whose_connection_moved_before_claim_is_refused_and_released(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given one DAS payment found by a successful consultation; the seed page's
    # money is its own, so the receipt is measured from its pinned budget here.
    das = _found_payment(world)
    key = _payment_key(das)
    budget = _pin_budget(world)
    baseline = _on_budget(_budgets(world), budget)
    # When its SP-COMP is reserved at v1, then a termo renewal moves the
    # connection to v2 before the claim.
    real = import_module("apps.integrations.call_protocol").reserve
    reserved: list[tuple[UUID, str, dict[str, Any]]] = []

    def reserve_then_renew(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        row = real(*args, **kwargs)
        reserved.append((row.pk, row.request_key, _target(world, row.pk)))
        _renew(world)
        return row

    monkeypatch.setattr(payments, "reserve", reserve_then_renew)
    refusals: list[tuple[UUID, str, str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        payments, "claim", _recording_claim(world, payments, refusals, lambda _id: None)
    )
    tenant, actor = world.termo.tenant.pk, world.termo.actor
    try:
        call = payments.emit_payment_receipt(tenant, das.pk, actor.pk)
    except AdmissionDeniedError:
        pytest.fail("the claim's refusal escaped the receipt worker")
    assert call is not None
    # Then the reservation is the payment's one intent, holding one priced slot
    # on the pinned budget...
    [(row_id, row_key, entry)] = reserved
    assert (row_key, _intent(entry)) == (key, (key, key)), (
        "the receipt call is not the payment's intent"
    )
    assert entry["call"]["state"] == CallState.RESERVED
    assert entry["observations"] == []
    assert _slots(entry) == [(1, "reserved", RECEIPT_MICROBRL)]
    assert _on_budget(entry, budget) == (
        baseline[0] + RECEIPT_MICROBRL,
        *baseline[1:],
    )
    # ...and the claim refused that same call, typed as moved, changing exactly
    # its state, code and version, releasing that slot, returning the money.
    [(refused_id, name, state, reason, refused)] = refusals
    assert (refused_id, name, state, reason) == (
        row_id,
        "VersionMovedError",
        CallState.AUTHORITY,
        CallState.AUTHORITY,
    )
    version = entry["call"]["version"]
    assert _delta(entry["call"], refused["call"]) == {
        "provider_code": (entry["call"]["provider_code"], CallState.AUTHORITY),
        "state": (CallState.RESERVED, CallState.AUTHORITY),
        "version": (version, version + 1),
    }
    assert refused["observations"] == []
    [before], [after] = entry["slots"], refused["slots"]
    assert _delta(before, after) == {
        "state": ("reserved", "released"),
        "version": (before["version"], before["version"] + 1),
    }
    assert _on_budget(refused, budget) == baseline
    assert set(_delta(entry["budgets"][0], refused["budgets"][0])) <= {
        "reserved",
        "version",
    }
    # The worker returns that bound call as stored, unchanged since the refusal,
    # never the row as it was reserved.
    assert _target(world, call.pk) == refused
    assert (call.pk, call.request_key, call.business_effect_key) == (row_id, key, key)
    assert world.sent("/Emitir") == []
    assert (call.state, call.provider_code) == (
        "bloqueada_autoridade",
        "bloqueada_autoridade",
    )


@DB
def test_a_receipt_retry_after_a_401_refused_at_claim_is_reported_as_stored(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given one DAS payment found by a successful consultation.
    das = _found_payment(world)
    key = _payment_key(das)
    budget = _pin_budget(world)
    baseline = _on_budget(_budgets(world), budget)
    # When its first SP-COMP attempt gets a complete 401, and the connection
    # version moves at the retry's claim.
    protocol = import_module("apps.integrations.call_protocol")
    entries: list[dict[str, Any]] = []

    def renew_at_entry(call_id: UUID) -> None:
        entries.append(_target(world, call_id))
        _renew(world)

    refusals: list[tuple[UUID, str, str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        protocol, "claim", _recording_claim(world, protocol, refusals, renew_at_entry)
    )
    performs = _observe_perform(monkeypatch)
    world.script(Reply(status=401))
    tenant, actor = world.termo.tenant.pk, world.termo.actor
    try:
        call = payments.emit_payment_receipt(tenant, das.pk, actor.pk)
    except AdmissionDeniedError:
        pytest.fail("the retry claim's refusal escaped the receipt worker")
    # perform itself returns the 401's result once the retry claim's refusal is
    # recorded, before the receipt returns: it never hands that refusal on, so
    # the receipt's own handler cannot mask it (AXRM-045, gate review :730).
    assert [row[0] for row in performs] == ["returned"], (
        "perform raised the retry claim's refusal"
    )
    assert call is not None
    # Then the retry claim met the payment's intent with the definitive,
    # unbilled 401 and its slot released on the pinned budget...
    [entry] = entries
    assert _intent(entry) == (key, key), "the receipt call is not the payment's intent"
    [observation] = entry["observations"]
    assert {k: observation[k] for k in DEFINITIVE_401} == DEFINITIVE_401
    held = entry["call"]
    assert (
        held["state"],
        held["provider_code"],
        held["attempts"],
        held["http_status"],
        held["billed"],
    ) == (CallState.RETRY, COMPLETE_401, 1, 401, "nao")
    assert _slots(entry) == [(1, "released", RECEIPT_MICROBRL)]
    assert _on_budget(entry, budget) == baseline
    # ...perform returned exactly that 401's own result (gate review :739)...
    [(_returned, result)] = performs
    assert _the_401(result) == (
        CallState.FAILED,
        401,
        COMPLETE_401,
        "nao",
        0,
        observation["response_digest"],
        "",
    ), "perform did not return the 401's own result"
    # ...and the claim refused that call, typed as moved, changing exactly its
    # state, code and version: the 401 (attempt, fence, digest), the released
    # slot and the budget all identical, no second slot.
    [(refused_id, name, state, reason, refused)] = refusals
    assert (refused_id, name, state, reason) == (
        call.pk,
        "VersionMovedError",
        CallState.AUTHORITY,
        CallState.AUTHORITY,
    )
    assert _delta(held, refused["call"]) == {
        "provider_code": (COMPLETE_401, CallState.AUTHORITY),
        "state": (CallState.RETRY, CallState.AUTHORITY),
        "version": (held["version"], held["version"] + 1),
    }
    assert refused["observations"] == entry["observations"]
    assert refused["slots"] == entry["slots"]
    assert refused["budgets"] == entry["budgets"]
    # The worker returns that bound call as stored, unchanged since the refusal,
    # and the one exchange is the original attempt's.
    assert _target(world, call.pk) == refused
    assert (call.request_key, call.business_effect_key) == (key, key)
    [sent] = world.sent("/Emitir")
    assert _tag(sent) == observation["attempt_id"]
    assert (call.state, call.provider_code) == (
        "bloqueada_autoridade",
        "bloqueada_autoridade",
    )


def _receipt_entry(
    entry: dict[str, Any],
    site: str,
    key: str,
    budget: tuple[object, ...],
    baseline: tuple[int, ...],
) -> None:
    """The receipt claim's entry: reserved with one priced slot, or after the
    definitive 401 with that slot released, on the pinned budget."""
    assert _intent(entry) == (key, key), "the receipt call is not the payment's intent"
    held = entry["call"]
    if site == "claim":
        assert held["state"] == CallState.RESERVED
        assert entry["observations"] == []
        assert _slots(entry) == [(1, "reserved", RECEIPT_MICROBRL)]
        assert _on_budget(entry, budget) == (
            baseline[0] + RECEIPT_MICROBRL,
            *baseline[1:],
        )
        return
    assert (
        held["state"],
        held["provider_code"],
        held["attempts"],
        held["http_status"],
        held["billed"],
    ) == (CallState.RETRY, COMPLETE_401, 1, 401, "nao")
    [observation] = entry["observations"]
    assert {k: observation[k] for k in DEFINITIVE_401} == DEFINITIVE_401
    assert _slots(entry) == [(1, "released", RECEIPT_MICROBRL)]
    assert _on_budget(entry, budget) == baseline


@DB
@pytest.mark.parametrize("site", ["claim", "retry-claim"])
@pytest.mark.parametrize("cause", ["authority-revoked", "termo-expired"])
def test_an_ordinary_authority_veto_at_a_receipt_claim_is_never_a_moved_refusal(
    world: World, monkeypatch: pytest.MonkeyPatch, cause: str, site: str
) -> None:
    # Given one DAS payment found by a successful consultation, and for the
    # retry site a complete first 401.
    das = _found_payment(world)
    key = _payment_key(das)
    budget = _pin_budget(world)
    baseline = _on_budget(_budgets(world), budget)
    if site == "retry-claim":
        world.script(Reply(status=401))
    protocol = import_module("apps.integrations.call_protocol")
    module = payments if site == "claim" else protocol
    tenant, actor = world.termo.tenant.pk, world.termo.actor
    entries: list[dict[str, Any]] = []
    reasons: list[str] = []

    def veto_at_entry(call_id: UUID) -> None:
        # When that claim meets an ordinary authority veto, no version moving:
        # the client's authority revoked, or the firm's termo no longer in force.
        entries.append(_target(world, call_id))
        with tenant_context(tenant):
            if cause == "authority-revoked":
                client = world.termo.client.pk
                head = head_record(client, "pagtoweb")
                assert head is not None
                revoke_authority(
                    actor, client, service="pagtoweb", expected_version=head.version
                )
                reasons.append(CLIENT_ABSENT)
            else:
                for name in ("work", "parties"):
                    found = import_module(f"apps.integrations.serpro.{name}")
                    monkeypatch.setattr(found, "termo_in_force", lambda *_args: False)
                connection = ProviderConnection.objects.get(
                    pk=world.termo.connection.pk
                )
                reasons.append(f"{TERMO_PREFIX}{connection.version}")

    refusals: list[tuple[UUID, str, str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        module, "claim", _recording_claim(world, module, refusals, veto_at_entry)
    )
    performs = _observe_perform(monkeypatch)
    public: tuple[str, ...]
    try:
        payments.emit_payment_receipt(tenant, das.pk, actor.pk)
        public = ("returned",)
    except AdmissionDeniedError as error:
        public = ("raised", type(error).__name__, error.reason)
    # Then that claim met the payment's intent in its expected state.
    [entry] = entries
    _receipt_entry(entry, site, key, budget, baseline)
    held = entry["call"]
    # At that claim's exit the refusal is the plain one with its own cause, and
    # the claim rolled back: every row as at its entry, fence, lease, epoch and
    # version included.
    [(call_id, name, state, reason, at_exit)] = refusals
    assert (name, state, reason) == (
        "AdmissionDeniedError",
        CallState.AUTHORITY,
        *reasons,
    )
    assert [at_exit["call"][field] for field in LEASE] == [
        held[field] for field in LEASE
    ]
    assert at_exit == entry
    # At the retry site perform itself hands that same plain refusal on (E5,
    # gate review :730); the first claim has no perform.
    assert performs == ([] if site == "claim" else [("raised", name, reason)])
    # It reaches the receipt's caller, which leaves the call as it was and
    # sends nothing more.
    assert public == ("raised", name, reason)
    after = _target(world, call_id)
    assert [after["call"][field] for field in LEASE] == [held[field] for field in LEASE]
    assert after == entry
    assert len(world.sent("/Emitir")) == (0 if site == "claim" else 1)


def _page_scope(world: World, query: PaymentQuery) -> tuple[str, str]:
    """Page 0's key and body digest, derived from the query and termo alone."""
    assert query.window == support.WINDOW
    key = _sha("pagtoweb-pagina", query.request_key, "0")
    data: dict[str, Any] = {
        "intervaloDataArrecadacao": {
            "dataInicial": support.WINDOW.start.isoformat(),
            "dataFinal": support.WINDOW.end.isoformat(),
        },
        "primeiroDaPagina": 0,
        "tamanhoDaPagina": 100,
    }
    if support.WINDOW.revenue_codes:
        data["codigoReceitaLista"] = list(support.WINDOW.revenue_codes)
    with tenant_context(world.termo.tenant.pk):
        parties = termo_parties(world.termo.connection.pk, world.termo.client.cnpj)
    assert parties is not None
    body = build_request("SP-PAG", parties, data).body
    return key, hashlib.sha256(body).hexdigest()


def _record_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, list[tuple[object, ...]]]:
    """Call-through recorders on the page's two settlement routes."""
    routes: dict[str, list[tuple[object, ...]]] = {"refused": [], "settle": []}
    real_refused, real_settle = payments._refused, payments._settle_page

    def refused_route(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        query_arg, index, scope, page_key, error = args[:5]
        routes["refused"].append(
            (query_arg.pk, index, scope, page_key, f"{type(error).__name__}: {error}")
        )
        return real_refused(*args, **kwargs)

    def settle_route(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        query_arg, index, scope, call_id = args[:4]
        routes["settle"].append((query_arg.pk, index, scope, call_id))
        return real_settle(*args, **kwargs)

    monkeypatch.setattr(payments, "_refused", refused_route)
    monkeypatch.setattr(payments, "_settle_page", settle_route)
    return routes


def _page_routes(
    site: str, query_id: UUID, key: str, digest: str, target: dict[str, Any]
) -> tuple[list[object], dict[str, list[tuple[object, ...]]]]:
    """What perform returns and which settlement route the page takes, per site."""
    call_id = target["call"]["id"]
    if site == "claim":
        moved = f"VersionMovedError: {CallState.AUTHORITY}"
        return [], {"refused": [(query_id, 0, digest, key, moved)], "settle": []}
    [observation] = target["observations"]
    result = (
        CallState.FAILED,
        401,
        COMPLETE_401,
        "nao",
        0,
        observation["response_digest"],
        "",
    )
    return [result], {"refused": [], "settle": [(query_id, 0, digest, call_id)]}


@DB
@pytest.mark.parametrize("site", ["claim", "retry-claim"])
def test_a_page_moved_at_its_claim_settles_its_own_query_from_the_refusal(
    world: World, monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    # Given one consultation opened, its id and key held, its page 0 scope
    # derived before the run, and for the retry site a complete first 401.
    tenant, client = world.termo.tenant.pk, world.termo.client
    with tenant_context(tenant):
        locked = ClientCompany.objects.select_for_update().get(pk=client.pk)
        query = payments.open_payment_query(
            locked, support.WINDOW, actor=None, nonce="consulta-1"
        )
    pinned = (query.pk, query.request_key)
    key, digest = _page_scope(world, query)
    if site == "retry-claim":
        world.script(Reply(status=401))
    budget = _pin_budget(world)
    baseline = _on_budget(_budgets(world), budget)
    # When the connection version moves at that page's claim.
    protocol = import_module("apps.integrations.call_protocol")
    module = payments if site == "claim" else protocol
    refusals: list[tuple[UUID, str, str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        module,
        "claim",
        _recording_claim(world, module, refusals, lambda _id: _renew(world)),
    )
    performs = _observe_perform(monkeypatch)
    routes = _record_routes(monkeypatch)
    settled = payments.run_payment_query(tenant, query.pk)
    [call] = world.calls()
    sent = 0 if site == "claim" else 1
    stored = (
        CallState.AUTHORITY,
        CallState.AUTHORITY,
        sent,
        None if site == "claim" else 401,
        "desconhecido" if site == "claim" else "nao",
    )

    def receipts_of_the_client() -> list[tuple[str, UUID, str, str]]:
        with tenant_context(tenant):
            return list(
                FederalConsultation.objects.filter(client_id=client.pk).values_list(
                    "kind", "client_id", "scope_digest", "provider_ref"
                )
            )

    # Then that same query ends falha by the moved refusal's cause, with one
    # payments receipt for that scope citing that call, the call being page 0's
    # own intent; a duplicate run of it changes nothing.
    first_routes: dict[str, list[tuple[object, ...]]] = {}
    for run in ("first", "duplicate"):
        if run == "duplicate":
            first_routes = {name: list(rows) for name, rows in routes.items()}
            settled = payments.run_payment_query(tenant, query.pk)
        assert (settled.pk, settled.request_key) == pinned, run
        assert (settled.state, settled.failure_code) == (
            "falha",
            ADMISSION_REFUSED,
        ), run
        target = _target(world, call.pk)
        row = target["call"]
        assert (
            row["state"],
            row["provider_code"],
            row["attempts"],
            row["http_status"],
            row["billed"],
        ) == stored, run
        assert (
            row["request_key"],
            row["business_effect_key"],
            row["request_digest"],
        ) == (
            key,
            key,
            digest,
        ), run
        assert receipts_of_the_client() == [
            (FederalConsultationKind.PAGAMENTOS, client.pk, digest, str(call.pk))
        ], run
        assert [slot[:2] for slot in _slots(target)] == [(1, "released")], run
        assert _on_budget(target, budget) == baseline, run
        assert [other.pk for other in world.calls()] == [call.pk], run
        assert len(world.sent()) == sent, run
    if site == "retry-claim":
        [request] = world.sent()
        assert hashlib.sha256(request.body).hexdigest() == digest
        assert world.offsets() == [0]
    # And the claim raised the typed moved refusal for that call (named by string:
    # the class is absent on the red trees), settled through its own route:
    # `_refused` at the first claim, `_settle_page` after perform returned the
    # 401's own result at the retry; the duplicate took neither.
    assert [(row[0], row[1]) for row in refusals] == [(call.pk, "VersionMovedError")]
    expected_performs, expected = _page_routes(site, query.pk, key, digest, target)
    assert [
        _the_401(row[1]) if row[0] == "returned" else row for row in performs
    ] == expected_performs, "the page took the wrong settlement route"
    assert routes == expected, "the page took the wrong settlement route"
    assert routes == first_routes, "the duplicate run settled the page again"


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
def test_a_pause_at_the_receipt_first_claim_reaches_the_task(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,  # noqa: ANN401
) -> None:
    # Given one DAS payment found by a successful consultation.
    world.script(page([support.item("DAS000000000045")]))
    world.consult()
    with tenant_context(world.termo.tenant.pk):
        das = FederalPayment.objects.get()
    # When its SP-COMP is reserved, then a pause meets its first claim.
    real_claim = import_module("apps.integrations.call_protocol").claim

    def pause_then_claim(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        settings.SERPRO_PAUSED = True
        return real_claim(*args, **kwargs)

    monkeypatch.setattr(payments, "claim", pause_then_claim)
    tasks = import_module("apps.integrations.serpro.tasks")
    args = (str(world.termo.tenant.pk), str(das.pk), str(world.termo.actor.pk))
    try:
        outcome = tasks.emit_payment_receipt_task(*args)
    except AdmissionDeniedError as error:
        outcome = f"raised {error}"
    # Then the pause reaches the task, and the call stays reserved with its slot.
    with tenant_context(world.termo.tenant.pk):
        call = ProviderCall.objects.get(operation="SP-COMP")
        slots = list(
            ProviderReservation.objects.filter(call=call).values_list(
                "state", flat=True
            )
        )
    assert (outcome, call.state, slots) == (
        "raised pausada",
        "reservada",
        ["reserved"],
    )
    assert world.sent("/Emitir") == []
