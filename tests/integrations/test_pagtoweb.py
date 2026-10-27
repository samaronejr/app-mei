"""AXRM-015 acceptance: paging, matching, budget, 403, receipts and environment gate.

Every test drives the real protocol (reserve, claim, perform, finalize), the real
budget policy and the real transport against a loopback `FakeSerpro`.
"""

import json
import logging
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from pytest_django.fixtures import SettingsWrapper
from sentry_sdk.integrations.logging import EventHandler

from apps.accounts.models import User
from apps.audit.models import Event
from apps.clients.models import ClientCompany, TaxProfile, TaxRegime
from apps.core.tenancy import tenant_context
from apps.integrations.authority import head_record
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.models import IntegrationIssue
from apps.integrations.serpro.models import PaymentQuery
from apps.integrations.serpro.pagtoweb import PaymentRange
from apps.integrations.serpro.payments import (
    PaymentQueryConflictError,
    emit_payment_receipt,
    queue_payment_consultation,
    receipt_request_key,
    run_payment_query,
)
from apps.integrations.serpro.tasks import scheduled_window
from apps.obligations.federal import payment_from_provider
from apps.obligations.models import (
    Document,
    FederalConsultation,
    FederalPayment,
    Obligation,
    SourceObservation,
)
from apps.obligations.payment_matching import match_das_payments
from apps.tenants.models import Membership, TenantRole
from tests.fakes.provider import Reply
from tests.integrations.pagtoweb_support import (
    COMPETENCE,
    CONSULTA_MICROBRL,
    PDF,
    WINDOW,
    World,
    darfs,
    declare_and_verify,
    denied,
    item,
    page,
    receipt_body,
    world,
)
from tests.integrations.test_transport_privacy import (
    captured_events,
    production_console,
)

__all__ = ["world"]

pytestmark = pytest.mark.django_db(transaction=True)


def receipts(world: World) -> list[tuple[str, int]]:
    with tenant_context(world.termo.tenant.pk):
        return [
            (row.outcome, row.payments.count())
            for row in FederalConsultation.objects.order_by("created_at", "pk")
        ]


def payments(world: World) -> list[FederalPayment]:
    with tenant_context(world.termo.tenant.pk):
        return list(FederalPayment.objects.order_by("numero_documento"))


def test_two_pages_of_100_and_3_store_103_payments_and_match_one_das(
    world: World,
) -> None:
    # Given the Receita holds 102 unrelated DARFs and one DAS for September/2026.
    first = darfs(100)
    second = [*darfs(2, start=100), item("DAS000000000001", total=81.05)]
    world.script(page(first), page(second))
    # When the firm's consultation runs.
    query = world.consult()
    # Then two billed page calls (offsets 0 and 100) made one complete answer.
    assert (query.state, query.pages_fetched, query.payments_found) == (
        "encontrado",
        2,
        103,
    )
    assert query.matched_count == 1
    assert world.offsets() == [0, 100]
    assert [(c.state, c.billed) for c in world.calls()] == [("sucesso", "sim")] * 2
    assert receipts(world) == [("encontrado", 100), ("encontrado", 3)]
    stored = payments(world)
    assert len(stored) == 103
    [das] = [row for row in stored if row.tipo == "DAS"]
    assert das.matched_obligation_id == world.obligation.pk
    assert das.valor_total == Decimal("81.05")
    assert all(row.matched_obligation_id is None for row in stored if row is not das)
    assert world.budget().known_liability == 2 * CONSULTA_MICROBRL


@pytest.mark.parametrize(
    ("sizes", "state", "pages"),
    [
        ((0,), "nada_localizado", 1),
        ((99,), "encontrado", 1),
        ((100, 0), "encontrado", 2),
        ((100, 100, 7), "encontrado", 3),
    ],
    ids=["empty", "short", "exact-hundred", "three-pages"],
)
def test_paging_stops_at_the_first_short_page(
    world: World, sizes: tuple[int, ...], state: str, pages: int
) -> None:
    start = 0
    for size in sizes:
        world.script(page(darfs(size, start=start)))
        start += size
    query = world.consult()
    assert (query.state, query.pages_fetched) == (state, pages)
    assert world.offsets() == [100 * n for n in range(pages)]
    assert query.payments_found == sum(sizes)


def test_ten_full_pages_are_incomplete_and_no_eleventh_page_is_sent(
    world: World,
) -> None:
    with tenant_context(world.termo.tenant.pk):
        ProviderBudget.objects.update(cap_microbrl=20 * CONSULTA_MICROBRL)
    world.script(*(page(darfs(100, start=100 * n)) for n in range(11)))
    query = world.consult()
    assert (query.state, query.failure_code, query.pages_fetched) == (
        "falha",
        "limite_de_paginas",
        10,
    )
    assert len(world.sent()) == 10
    assert query.payments_found == 1000
    with tenant_context(world.termo.tenant.pk):
        issue = IntegrationIssue.objects.get(reason="consulta_pagamentos_falhou")
    assert str(query.pk) in issue.evidence_refs


def test_nothing_found_is_a_receipt_never_a_non_payment_fact(world: World) -> None:
    world.script(page([]))
    query = world.consult()
    assert query.state == "nada_localizado"
    assert receipts(world) == [("nada_localizado", 0)]
    with tenant_context(world.termo.tenant.pk):
        assert not SourceObservation.objects.exists()
        obligation = Obligation.objects.get(pk=world.obligation.pk)
    assert obligation.status == world.obligation.status


def test_budget_refusal_mid_paging_keeps_fetched_pages_and_ends_falha(
    world: World,
) -> None:
    # Given the firm's cap admits exactly one more consultation page.
    with tenant_context(world.termo.tenant.pk):
        ProviderBudget.objects.update(cap_microbrl=CONSULTA_MICROBRL)
    world.script(page(darfs(100)), page(darfs(3, start=100)))
    query = world.consult()
    # Then page 2 never left; page 1's 100 payments stay with their receipt.
    assert (query.state, query.failure_code, query.pages_fetched) == (
        "falha",
        "cota_esgotada_parcial",
        1,
    )
    assert len(world.sent()) == 1
    assert receipts(world) == [("encontrado", 100), ("cota_esgotada", 0)]
    assert len(payments(world)) == 100
    assert [c.state for c in world.calls()] == ["sucesso", "bloqueada_cota"]
    budget = world.budget()
    assert (budget.known_liability, budget.reserved) == (CONSULTA_MICROBRL, 0)


def test_budget_refusal_on_the_first_page_is_cota_esgotada(world: World) -> None:
    with tenant_context(world.termo.tenant.pk):
        ProviderBudget.objects.update(cap_microbrl=0)
    query = world.consult()
    assert (query.state, query.pages_fetched) == ("cota_esgotada", 0)
    assert world.sent() == []
    assert receipts(world) == [("cota_esgotada", 0)]


@pytest.mark.parametrize(
    "body",
    [None, b"<html>acesso negado</html>"],
    ids=["documented-403", "non-json-403"],
)
def test_a_403_is_billed_once_flags_authority_and_blocks_the_rerun(
    world: World, body: bytes | None
) -> None:
    world.script(denied(body))
    query = world.consult()
    # Then one billed call, absence recorded from that call, owned work opened.
    assert query.state == "sem_procuracao"
    [call] = world.calls()
    assert (call.state, call.billed, call.http_status) == ("negada", "sim", 403)
    assert world.budget().known_liability == CONSULTA_MICROBRL
    with tenant_context(world.termo.tenant.pk):
        head = head_record(world.termo.client.pk, "pagtoweb")
        assert head is not None
        assert (head.status, head.source_call_ref) == (
            "ausente_detectada",
            str(call.pk),
        )
        assert IntegrationIssue.objects.filter(
            reason="autorizacao_ausente", service="pagtoweb", state="aberta"
        ).exists()
    assert receipts(world) == [("sem_procuracao", 0)]
    # When staff run it again without re-declaring the authority.
    rerun = world.consult(nonce="consulta-2")
    # Then admission refuses before any byte: still exactly one wire request.
    assert (rerun.state, rerun.failure_code) == (
        "sem_procuracao",
        "autorizacao_ausente",
    )
    assert len(world.sent()) == 1
    assert len(world.calls()) == 1
    # And only a fresh declaration plus verification readmits a deliberate rerun.
    declare_and_verify(world.termo)
    world.script(page([]))
    assert world.consult(nonce="consulta-3").state == "nada_localizado"
    assert len(world.sent()) == 2


def test_a_malformed_success_is_a_billed_falha_with_its_receipt(world: World) -> None:
    world.script(Reply(body=b'{"status":200,"mensagens":[],"dados":"{}"}'))
    query = world.consult()
    assert (query.state, query.failure_code) == ("falha", "resposta_pagtoweb_invalida")
    [call] = world.calls()
    assert (call.state, call.billed) == ("falhou", "sim")
    assert receipts(world) == [("falha", 0)]
    assert payments(world) == []


def test_a_5xx_is_incerto_and_never_retried(world: World) -> None:
    world.script(Reply(status=503, body=b""))
    query = world.consult()
    assert query.state == "incerto"
    assert len(world.sent()) == 1
    assert receipts(world) == [("incerto", 0)]


def test_fake_and_trial_answers_never_become_official_observations(
    world: World,
) -> None:
    world.script(page([item("DAS000000000002")]))
    assert world.consult().matched_count == 1
    [das] = payments(world)
    assert das.matched_obligation_id == world.obligation.pk
    with tenant_context(world.termo.tenant.pk):
        assert not SourceObservation.objects.exists()


def _record(numero: str, *, tipo: str = "09", periodo: str = "2026-09-01") -> object:
    return payment_from_provider(
        {
            "numeroDocumento": numero,
            "tipo": {"09": "DAS", "04": "DARF"}[tipo],
            "periodoApuracao": periodo,
            "dataArrecadacao": "2026-10-20",
            "receita": {"codigo": "1001"},
            "valorTotal": 1,
        }
    )


def _profile(client: ClientCompany, regime: str, valid_from: date) -> None:
    TaxProfile.objects.create(
        tenant_id=client.tenant_id,
        client=client,
        regime=regime,
        valid_from=valid_from,
        provenance="staff_entry",
    )


@pytest.mark.parametrize(
    ("case", "matched"),
    [
        ("mei-das-same-month", True),
        ("unequal-amount-still-matches", True),
        ("darf-same-month", False),
        ("das-other-month", False),
        ("non-mei-flag", False),
        ("timeline-says-simples", False),
        ("timeline-gap-is-unknown", False),
        ("no-das-obligation", False),
    ],
)
def test_match_truth_table(world: World, case: str, matched: bool) -> None:
    client = world.termo.client
    record = _record("M1")
    with tenant_context(world.termo.tenant.pk):
        match case:
            case "darf-same-month":
                record = _record("M1", tipo="04")
            case "das-other-month":
                record = _record("M1", periodo="2026-08-01")
            case "non-mei-flag":
                ClientCompany.objects.filter(pk=client.pk).update(is_mei=False)
            case "timeline-says-simples":
                _profile(client, TaxRegime.SIMPLES, date(2026, 1, 1))
            case "timeline-gap-is-unknown":
                _profile(client, TaxRegime.MEI, date(2026, 10, 1))
            case "no-das-obligation":
                Obligation.objects.filter(pk=world.obligation.pk).delete()
        fresh = ClientCompany.objects.get(pk=client.pk)
        with CaptureQueriesContext(connection) as queries:
            [result] = match_das_payments(fresh, [record])  # type: ignore[list-item]
    assert (result.matched_obligation_id == world.obligation.pk) is matched
    assert len(queries) <= 3


def test_another_clients_das_obligation_is_never_matched(world: World) -> None:
    tenant = world.termo.tenant
    with tenant_context(tenant.pk):
        other = ClientCompany.objects.create(
            tenant=tenant,
            legal_name="Outro sintetico",
            cnpj="11444777000161",
            is_mei=True,
        )
        [result] = match_das_payments(other, [_record("M2")])  # type: ignore[list-item]
    assert result.matched_obligation_id is None


def test_a_document_on_file_keeps_its_first_association(world: World) -> None:
    # Given the DAS was first recorded before its obligation existed.
    with tenant_context(world.termo.tenant.pk):
        Obligation.objects.filter(pk=world.obligation.pk).delete()
    world.script(page([item("DAS000000000003")]))
    assert world.consult().matched_count == 0
    with tenant_context(world.termo.tenant.pk):
        Obligation.objects.create(
            tenant=world.termo.tenant,
            client=world.termo.client,
            obligation_type_id="DAS",
            competence_month=COMPETENCE,
            nominal_due_date=date(2026, 10, 20),
            resolved_due_date=date(2026, 10, 20),
        )
    # When a later consultation returns the same document.
    world.script(page([item("DAS000000000003")]))
    second = world.consult(nonce="consulta-2")
    # Then the append-only payment agrees with itself: no conflict, no rewrite.
    assert (second.state, second.matched_count) == ("encontrado", 0)
    [das] = payments(world)
    assert das.matched_obligation_id is None


def _unassigned_accountant(world: World) -> User:
    user = User.objects.create_user(email=f"staff-{uuid4().hex}@example.test")
    Membership.objects.create(
        tenant=world.termo.tenant,
        user=user,
        role=TenantRole.STAFF_ACCOUNTANT,
        client=None,
    )
    return user


def test_two_staff_clicks_on_the_same_render_queue_one_consultation(
    world: World, django_capture_on_commit_callbacks: object
) -> None:
    actor = world.termo.actor
    with tenant_context(world.termo.tenant.pk):
        with django_capture_on_commit_callbacks() as callbacks:  # type: ignore[operator]
            first = queue_payment_consultation(
                actor, world.termo.client.pk, WINDOW, expected_latest=None, nonce="a"
            )
        with pytest.raises(PaymentQueryConflictError):
            queue_payment_consultation(
                actor, world.termo.client.pk, WINDOW, expected_latest=None, nonce="b"
            )
        assert PaymentQuery.objects.count() == 1
        assert (
            Event.objects.filter(
                action="integrations.payment_consultation_requested"
            ).count()
            == 1
        )
    assert first.state == "na_fila"
    assert len(callbacks) == 1
    assert world.sent() == []


def test_an_unassigned_accountant_is_refused_with_no_rows(world: World) -> None:
    from django.http import Http404  # noqa: PLC0415

    outsider = _unassigned_accountant(world)
    with tenant_context(world.termo.tenant.pk), pytest.raises(Http404):
        queue_payment_consultation(
            outsider, world.termo.client.pk, WINDOW, expected_latest=None, nonce="x"
        )
    with tenant_context(world.termo.tenant.pk):
        assert not PaymentQuery.objects.exists()


def test_a_queued_consultation_whose_initiator_lost_access_sends_nothing(
    world: World,
) -> None:
    actor = world.termo.actor
    with tenant_context(world.termo.tenant.pk):
        query = queue_payment_consultation(
            actor, world.termo.client.pk, WINDOW, expected_latest=None, nonce="c"
        )
        Membership.objects.filter(user=actor).update(role=TenantRole.STAFF_ACCOUNTANT)
    finished = run_payment_query(world.termo.tenant.pk, query.pk)
    assert (finished.state, finished.failure_code) == (
        "falha",
        "solicitante_sem_permissao",
    )
    assert world.sent() == []


def test_a_redelivered_run_continues_and_never_rebills_a_page(world: World) -> None:
    world.script(page(darfs(100)), page(darfs(1, start=100)))
    query = world.consult()
    again = run_payment_query(world.termo.tenant.pk, query.pk)
    assert again.state == query.state == "encontrado"
    assert len(world.sent()) == 2


def test_receipt_is_issued_once_and_stored_privately(world: World) -> None:
    world.script(page([item("DAS000000000004")]))
    world.consult()
    [das] = payments(world)
    encoded = json.dumps({"pdf": __import__("base64").b64encode(PDF).decode()})
    world.script(Reply(body=receipt_body(encoded)))
    call = emit_payment_receipt(world.termo.tenant.pk, das.pk, world.termo.actor.pk)
    assert call is not None
    assert call.state == "sucesso"
    assert call.request_key == receipt_request_key(das.pk)
    with tenant_context(world.termo.tenant.pk):
        document = Document.objects.get(acquisition_receipt_ref=str(call.pk))
    assert (
        document.uploaded_by_id,
        document.portal_visible,
        document.client_upload,
        document.acquisition_origin,
    ) == (None, False, False, "provedor_federal")
    # A second click or a redelivered task returns the same call: no new request.
    repeat = emit_payment_receipt(world.termo.tenant.pk, das.pk, world.termo.actor.pk)
    assert repeat is not None
    assert repeat.pk == call.pk
    assert len(world.sent("/Emitir")) == 1


def test_a_malformed_receipt_stores_nothing_and_is_not_retried(world: World) -> None:
    world.script(page([item("DAS000000000005")]))
    world.consult()
    [das] = payments(world)
    world.script(Reply(body=receipt_body("bm90IGEgcGRm")))
    call = emit_payment_receipt(world.termo.tenant.pk, das.pk, world.termo.actor.pk)
    assert call is not None
    assert (call.state, call.billed) == ("falhou", "sim")
    with tenant_context(world.termo.tenant.pk):
        assert not Document.objects.filter(
            acquisition_origin="provedor_federal"
        ).exists()
    emit_payment_receipt(world.termo.tenant.pk, das.pk, world.termo.actor.pk)
    assert len(world.sent("/Emitir")) == 1


def test_a_receipt_the_scanner_cannot_take_is_kept_billed_and_not_reissued(
    world: World, settings: SettingsWrapper
) -> None:
    world.script(page([item("DAS000000000007")]))
    world.consult()
    [das] = payments(world)
    settings.CLAMAV_ENABLED = False
    encoded = json.dumps({"pdf": __import__("base64").b64encode(PDF).decode()})
    world.script(Reply(body=receipt_body(encoded)))
    call = emit_payment_receipt(world.termo.tenant.pk, das.pk, world.termo.actor.pk)
    assert call is not None
    assert (call.state, call.billed) == ("sucesso", "sim")
    with tenant_context(world.termo.tenant.pk):
        assert not Document.objects.filter(
            acquisition_receipt_ref=str(call.pk)
        ).exists()
    emit_payment_receipt(world.termo.tenant.pk, das.pk, world.termo.actor.pk)
    assert len(world.sent("/Emitir")) == 1


def test_a_receipt_for_an_unassigned_accountant_sends_nothing(world: World) -> None:
    world.script(page([item("DAS000000000006")]))
    world.consult()
    [das] = payments(world)
    outsider = _unassigned_accountant(world)
    assert emit_payment_receipt(world.termo.tenant.pk, das.pk, outsider.pk) is None
    assert world.sent("/Emitir") == []
    assert len(world.calls("SP-COMP")) == 0


def test_the_schedule_asks_after_the_20th_for_the_previous_month() -> None:
    assert scheduled_window(date(2026, 10, 20)) is None
    assert scheduled_window(date(2026, 10, 21)) == PaymentRange(
        date(2026, 9, 1), date(2026, 10, 21)
    )
    assert scheduled_window(date(2027, 1, 25)) == PaymentRange(
        date(2026, 12, 1), date(2027, 1, 25)
    )


def test_wire_bearer_and_provider_text_never_reach_console_or_sentry(
    world: World,
) -> None:
    canary = "synthetic-pagtoweb-sentry-canary-5521"
    body = json.dumps(
        {
            "status": 403,
            "mensagens": [{"codigo": "Erro", "texto": f"Bearer {canary}"}],
            "dados": canary,
        }
    ).encode()
    world.script(
        Reply(body=b'{"status":200,"mensagens":[],"dados":"' + canary.encode() + b'"}'),
        denied(body),
    )
    states: list[str] = []

    def run(_logger: logging.Logger) -> None:
        states.append(world.consult().state)
        states.append(world.consult(nonce="consulta-2").state)

    # Given todo 11's real production console filter on every logger, and a real
    # Sentry client whose logging handler sees every record of the same run.
    def emit() -> None:
        root = logging.getLogger()
        handler = EventHandler(level=logging.DEBUG)
        root.addHandler(handler)
        try:
            console.append(production_console(run, name="", level=logging.DEBUG))
        finally:
            root.removeHandler(handler)

    console: list[str] = []
    events = captured_events(emit)
    # Then the run happened (a billed falha, then a billed 403) ...
    assert states == ["falha", "sem_procuracao"]
    assert "PAGTOWEB consultation finished" in console[0]
    # ... and neither the wire bearer nor provider text reached either sink.
    rendered = console[0] + json.dumps(events, default=str)
    assert events
    for secret in (canary, "synthetic-serpro", "Bearer"):
        assert secret not in rendered


def test_no_token_or_body_reaches_logs_or_persisted_rows(
    world: World, caplog: pytest.LogCaptureFixture, settings: SettingsWrapper
) -> None:
    canary = "synthetic-pagtoweb-canary-7731"
    caplog.set_level(logging.DEBUG)
    body = json.dumps(
        {
            "status": 403,
            "mensagens": [{"codigo": "Erro", "texto": f"Bearer {canary}"}],
            "dados": canary,
        }
    ).encode()
    world.script(
        Reply(body=b'{"status":200,"mensagens":[],"dados":"' + canary.encode() + b'"}')
    )
    world.consult()
    world.script(denied(body))
    world.consult(nonce="consulta-2")
    assert canary not in caplog.text
    with tenant_context(world.termo.tenant.pk):
        persisted = repr(
            [
                *PaymentQuery.objects.values(),
                *FederalConsultation.objects.values(),
                *Event.objects.values(),
                *IntegrationIssue.objects.values(),
            ]
        )
    assert canary not in persisted
    assert "Bearer" not in persisted
    assert "synthetic-serpro" not in persisted + caplog.text
