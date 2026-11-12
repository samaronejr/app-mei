"""AXRM-019 edges: each case pins one guard, branch or side effect of the Focus code.

Pure cases need no database; the rest reuse `test_focus_adapter.world`.
"""

import datetime as dt
import hashlib
import json
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from django.core.management.base import CommandError
from pytest_django.fixtures import SettingsWrapper

from apps.audit.models import Event
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.integrations import call_protocol, transport
from apps.integrations.allowlist import resolve
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.call_contracts import AdmissionDeniedError, StaleClaimError
from apps.integrations.call_dispatch import Dispatch
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_protocol import reserve
from apps.integrations.focus import adapter as focus_adapter
from apps.integrations.focus import company as focus_company
from apps.integrations.focus.adapter import (
    FAKE_TOKEN,
    MISSING_CREDENTIAL,
    RATE_LIMITED,
    FocusAdapter,
    _normalized_cnpj,
    _reported_day,
    classify,
    company_request,
    limiter_key,
    parse_company,
    request_digest,
    secret_variable,
    token_for,
    update_request,
)
from apps.integrations.focus.company import (
    FocusRefusedError as FocusRefused,
)
from apps.integrations.focus.company import (
    plan_setup,
    sync_company_status,
)
from apps.integrations.focus.qualification import qualify_empresa_status
from apps.integrations.focus.rate_limit import TokenRateLimiter
from apps.integrations.models import (
    AuthorityRecord,
    ClientProviderLink,
    ProviderConnection,
)
from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import (
    OperacaoNaoPermitida,
    ProviderRequest,
    ProviderResponse,
)
from apps.tenants.models import Tenant
from tests.fakes.provider import Reply
from tests.integrations.test_focus_adapter import (
    CNPJ,
    COMPANY_ID,
    MASTER_REF,
    OTHER_CNPJ,
    QUALIFICATION_REF,
    World,
    _capture,
    _capture_request,
    activate,
    disable,
    setup,
    world,
)

__all__ = ["world"]
db = pytest.mark.django_db(transaction=True)
NAME_A = "focus/a"
NAME_B = "focus/b"
SUBJECT = str(COMPANY_ID)


# --- Pure: request building, credentials, parsing, classification ---------------


def test_update_request_refuses_an_empty_field_set() -> None:
    with pytest.raises(OperacaoNaoPermitida):
        update_request(SUBJECT, {})


def test_request_digest_covers_variant_subject_query_and_body() -> None:
    fields: dict[str, bool | str] = {"certificado_especifico": True}
    base = update_request(SUBJECT, fields)
    variants = {
        request_digest(base),
        request_digest(update_request(SUBJECT, fields, dry_run=True)),
        request_digest(update_request("4322", fields)),
        request_digest(update_request(SUBJECT, {"certificado_especifico": False})),
        request_digest(company_request(SUBJECT)),
    }
    assert len(variants) == 5


def test_tokens_come_only_from_the_named_deployment_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert secret_variable("focus/token_master") == "FOCUS_SECRET_FOCUS_TOKEN_MASTER"
    named = ProviderConnection(environment="homologacao", secret_ref=MASTER_REF)
    unnamed = ProviderConnection(environment="homologacao", secret_ref="")
    monkeypatch.setenv("FOCUS_SECRET_FOCUS_TOKEN_MASTER", "")
    monkeypatch.setenv("FOCUS_SECRET_", "never-used")
    assert token_for(named) is None
    assert token_for(unnamed) is None
    monkeypatch.setenv("FOCUS_SECRET_FOCUS_TOKEN_MASTER", "deployment-value")
    assert token_for(named) == "deployment-value"
    assert (
        token_for(ProviderConnection(environment="fake", secret_ref="")) == FAKE_TOKEN
    )


def test_cnpj_normalization_accepts_only_cnpj_shapes() -> None:
    assert _normalized_cnpj("11.222.333/0001-81") == CNPJ
    assert _normalized_cnpj("12.abc.345/01de-35") == "12ABC34501DE35"
    for value in ("not-a-cnpj", "1122233300018", None, 11222333000181):
        assert _normalized_cnpj(value) == ""


def test_reported_days_are_local_calendar_days() -> None:
    assert _reported_day("") is None
    assert _reported_day(None) is None
    assert _reported_day("2026-11-12") == dt.date(2026, 11, 12)
    # 23:30 at -05:00 is 01:30 next day in America/Sao_Paulo.
    assert _reported_day("2026-11-12T23:30:00-05:00") == dt.date(2026, 11, 13)
    with pytest.raises(ValueError, match=r"^$"):
        _reported_day("2026-11-12T10:00:00")
    with pytest.raises(TypeError):
        _reported_day(20261112)


@pytest.mark.parametrize(
    ("method", "kind", "status", "state"),
    [
        ("GET", "configuration", 200, CallState.SUCCESS),
        ("GET", "configuration", 201, CallState.FAILED),
        ("GET", "configuration", 404, CallState.FAILED),
        ("GET", "configuration", 499, CallState.FAILED),
        ("GET", "read", 499, CallState.FAILED),
        ("PUT", "configuration", 499, CallState.FAILED),
        ("GET", "configuration", 429, CallState.FAILED),
        ("GET", "configuration", 500, CallState.FAILED),
        ("GET", "read", 429, CallState.RETRY),
        ("GET", "read", 503, CallState.RETRY),
        ("GET", "read", 422, CallState.FAILED),
        ("PUT", "configuration", 200, CallState.SUCCESS),
        ("PUT", "configuration", 422, CallState.FAILED),
        ("PUT", "configuration", 429, CallState.FAILED),
        ("PUT", "configuration", 500, CallState.UNCERTAIN),
        ("PUT", "configuration", 503, CallState.UNCERTAIN),
    ],
)
def test_classify_outcomes(method: str, kind: str, status: int, state: str) -> None:
    result = classify(
        "GET" if method == "GET" else "PUT", kind, ProviderResponse(status, b"{}")
    )
    assert (result.state, result.http_status, result.billed) == (state, status, "nao")
    assert result.provider_code == f"focus_http_{status}"


def _dispatch(request: ProviderRequest, operation: str, kind: str) -> Dispatch:
    receipt = SimpleNamespace(
        request_digest=request_digest(request),
        operation=operation,
        operation_class=kind,
    )
    return cast("Dispatch", SimpleNamespace(receipt=receipt))


def _adapter(request: ProviderRequest, token: str | None = FAKE_TOKEN) -> FocusAdapter:
    return FocusAdapter(
        ProviderTransport("focus", "fake", "http://127.0.0.1:1"),
        request,
        token=token,
        limiter_key="fake:edges",
    )


def test_unsent_refusals_never_touch_the_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = company_request(SUBJECT)
    # A different journaled request.
    other = _dispatch(
        update_request(SUBJECT, {"certificado_especifico": True}),
        "FC-EMP",
        "configuration",
    )
    assert _adapter(request).send(other).provider_code == "pedido_divergente"
    # No token.
    result = _adapter(request, None).send(_dispatch(request, "FC-EMP", "configuration"))
    assert (result.state, result.provider_code) == (
        CallState.FAILED,
        MISSING_CREDENTIAL,
    )
    # A full window: a read waits, configuration fails; neither is uncertain.
    monkeypatch.setattr(focus_adapter, "LIMITER", TokenRateLimiter(limit=0))
    read = _adapter(request).send(_dispatch(request, "FC-NFE", "read"))
    assert (read.state, read.provider_code) == (CallState.RETRY, RATE_LIMITED)
    config = _adapter(request).send(_dispatch(request, "FC-EMP", "configuration"))
    assert (config.state, config.provider_code) == (CallState.FAILED, RATE_LIMITED)


def test_allowlist_refusal_inside_send_is_a_definitive_unsent_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(focus_adapter, "LIMITER", TokenRateLimiter())
    request = ProviderRequest(
        variant="update", subject=SUBJECT, body=b'{"senha_certificado":"x"}'
    )
    result = _adapter(request).send(_dispatch(request, "FC-EMP", "configuration"))
    assert (result.state, result.provider_code, result.http_status) == (
        CallState.FAILED,
        "operacao_nao_permitida",
        None,
    )


def test_allowlist_takes_the_production_switch_and_only_the_exact_dry_run(
    settings: SettingsWrapper,
) -> None:
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = QUALIFICATION_REF
    body = json.dumps({"habilita_manifestacao": True}).encode()
    route, path = resolve(
        "focus", "FC-EMP", ProviderRequest(variant="update", subject=SUBJECT, body=body)
    )
    assert (route.method, path) == ("PUT", f"/v2/empresas/{SUBJECT}")
    _, dry = resolve(
        "focus",
        "FC-EMP",
        ProviderRequest(
            variant="update", subject=SUBJECT, body=body, query=(("dry_run", "1"),)
        ),
    )
    assert dry == f"/v2/empresas/{SUBJECT}?dry_run=1"
    refused = [
        (
            "FC-EMP",
            ProviderRequest(variant="read", subject=SUBJECT, query=(("dry_run", "1"),)),
        ),
        ("FC-NFE", ProviderRequest(query=(("dry_run", "1"),))),
        (
            "FC-EMP",
            ProviderRequest(
                variant="update", subject=SUBJECT, body=body, query=(("dry_run", "0"),)
            ),
        ),
        (
            "FC-EMP",
            ProviderRequest(
                variant="update",
                subject=SUBJECT,
                body=body,
                query=(("dry_run", "1"), ("versao", "1")),
            ),
        ),
    ]
    for code, request in refused:
        with pytest.raises(OperacaoNaoPermitida):
            resolve("focus", code, request)


# --- Database: sync side effects ---------------------------------------------------


@db
def test_sync_without_a_certificate_still_stamps_and_versions(world: World) -> None:
    world.fake.company.update(
        certificado_cnpj=None, certificado_valido_de=None, certificado_valido_ate=None
    )
    before = world.reload().version
    sync_company_status(world.link)
    link = world.reload()
    assert link.version == before + 1
    assert link.certificate_synced_at is not None


@db
def test_oversized_company_reply_fails_without_writing(world: World) -> None:
    world.fake.script.append(Reply(status=200, body=b"x" * (5 * 1024 * 1024 + 1)))
    before = world.reload()
    outcome = sync_company_status(before)
    assert outcome.state == CallState.FAILED
    assert world.reload().version == before.version


@db
def test_a_claim_lost_during_perform_reports_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lost(*_args: object, **_kwargs: object) -> None:
        raise StaleClaimError

    monkeypatch.setattr(focus_company, "perform", lost)
    before = world.reload()
    outcome = sync_company_status(before)
    assert outcome.report is None
    assert outcome.state == CallState.CLAIMED
    assert world.reload().version == before.version
    assert world.fake.requests == []


@db
def test_status_sync_and_setup_are_audited(world: World) -> None:
    setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    with tenant_context(world.tenant.pk):
        actions = set(Event.objects.values_list("action", flat=True))
    assert {
        "integrations.focus_setup_requested",
        "integrations.focus_company_status_synced",
        "integrations.focus_setup_confirmed",
        "integrations.capture_start_set",
    } <= actions


@db
def test_unconfirmed_setup_is_audited(world: World) -> None:
    world.fake.apply_puts = False
    with pytest.raises(CommandError):
        setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    with tenant_context(world.tenant.pk):
        actions = set(Event.objects.values_list("action", flat=True))
    assert "integrations.focus_setup_unconfirmed" in actions
    assert "integrations.focus_setup_confirmed" not in actions


# --- Database: setup -----------------------------------------------------------------


@db
def test_setup_adds_streams_to_the_existing_ones(world: World) -> None:
    activate(world, streams=["nfe_recebidas"])
    before = world.reload().version
    setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    link = world.reload()
    assert link.streams == ["nfe_recebidas", "nfsen_recebidas"]
    assert link.version > before


@db
def test_setup_refuses_a_revoked_link_before_fencing_the_month(world: World) -> None:
    activate(world, status="revogada", capture_start_month=None)
    with pytest.raises(CommandError, match="revogado"):
        setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    assert world.fake.requests == []
    assert world.reload().capture_start_month is None


@db
def test_nfe_receipt_names_the_qualification_evidence(
    world: World, settings: SettingsWrapper
) -> None:
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = QUALIFICATION_REF
    setup(world, "--streams", "nfe_recebidas", "--start-month", "2026-11")
    receipt = json.loads(
        next(Path(".evidence").glob("AXRM-FOCUS-setup-*.txt")).read_text()
    )
    assert receipt["qualification_ref"] == QUALIFICATION_REF


@pytest.mark.parametrize("month", ["2026-1", "2026-011", "x2026-11"])
@db
def test_setup_refuses_months_outside_the_exact_shape(world: World, month: str) -> None:
    with pytest.raises(CommandError, match="AAAA-MM"):
        setup(world, "--streams", "nfsen_recebidas", "--start-month", month)
    assert world.fake.requests == []


@pytest.mark.parametrize("value", ["xAXRM-PQ-FOCUS-1", "AXRM-PQ-FOCUS-1 extra"])
@db
def test_setup_refuses_a_qualification_id_with_extra_text(
    world: World, settings: SettingsWrapper, value: str
) -> None:
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = value
    with pytest.raises(CommandError, match="PQ-FOCUS-1"):
        setup(world, "--streams", "nfe_recebidas", "--start-month", "2026-11")
    assert world.fake.requests == []


@db
def test_setup_without_a_budget_sends_nothing(world: World) -> None:
    with tenant_context(world.tenant.pk):
        ProviderBudget.objects.all().delete()
    with pytest.raises(CommandError, match="orçamento"):
        setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    assert world.fake.requests == []


@db
def test_setup_while_focus_is_paused_sends_nothing(
    world: World, settings: SettingsWrapper
) -> None:
    settings.FOCUS_PAUSED = True
    with pytest.raises(CommandError, match="admissão"):
        setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    assert world.fake.requests == []


@db
def test_dry_run_reports_no_unconfirmed_fields(world: World) -> None:
    output = setup(
        world, "--streams", "nfsen_recebidas", "--start-month", "2026-11", "--dry-run"
    )
    record = json.loads(output.splitlines()[1])
    assert (record["unconfirmed"], record["confirmed"]) == ([], False)


# --- Database: disable ----------------------------------------------------------------


@db
def test_disable_of_a_never_configured_link_turns_every_receipt_switch_off(
    world: World,
) -> None:
    disable(world)
    assert world.fake.bodies() == [
        {
            "habilita_manifestacao_homologacao": False,
            "habilita_nfsen_recebidas_homologacao": False,
        }
    ]
    assert world.reload().disablement_state == "desativada_no_provedor_confirmada"


@db
def test_disable_without_a_credential_changes_nothing_locally(
    world: World, settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    activate(world)
    settings.FOCUS_ENVIRONMENT = "homologacao"
    with tenant_context(world.tenant.pk):
        ProviderConnection.objects.update(environment="homologacao")
    monkeypatch.delenv("FOCUS_SECRET_FOCUS_TOKEN_MASTER", raising=False)
    with pytest.raises(CommandError, match="credencial"):
        disable(world)
    assert world.reload().disablement_state == "ativa"
    assert world.fake.requests == []


@db
def test_partial_disable_of_a_confirmed_link_still_turns_the_stream_off(
    world: World,
) -> None:
    activate(
        world,
        streams=["nfe_recebidas", "nfsen_recebidas"],
        disablement_state="desativada_no_provedor_confirmada",
    )
    before = world.reload().version
    disable(world, "--stream", "nfe_recebidas")
    assert world.fake.bodies() == [{"habilita_manifestacao_homologacao": False}]
    link = world.reload()
    assert link.streams == ["nfsen_recebidas"]
    assert link.version > before


@db
def test_partial_disable_drops_only_the_named_stream_before_the_write(
    world: World,
) -> None:
    # Given Focus has the NF-e switch on and will not apply the disable.
    activate(world, streams=["nfe_recebidas", "nfsen_recebidas"])
    world.fake.company["habilita_manifestacao_homologacao"] = True
    world.fake.apply_puts = False
    with pytest.raises(CommandError, match="habilita_manifestacao_homologacao"):
        disable(world, "--stream", "nfe_recebidas")
    assert world.fake.company["habilita_manifestacao_homologacao"] is True
    link = world.reload()
    assert link.streams == ["nfsen_recebidas"]
    assert link.disablement_state == "ativa"
    with tenant_context(world.tenant.pk):
        assert ClientProviderLink.objects.count() == 1


# --- Mutation witnesses (AXRM-019 mutation pass) -------------------------------------


def test_company_parse_keeps_only_allowlisted_switches_as_days() -> None:
    body = json.dumps(
        {
            "id": COMPANY_ID,
            "cnpj": CNPJ,
            "nome": "Empresa",
            "token_producao": "SYNTHETICPRODUCTIONTOKEN0000000000",
            "data_inicio_recebimento_nfsen": "2026-11-01T00:00:00-03:00",
            "habilita_nfe": False,
        }
    ).encode()
    report = parse_company(ProviderResponse(200, body), SUBJECT)
    assert report.flags == {
        "data_inicio_recebimento_nfsen": "2026-11-01",
        "habilita_nfe": False,
    }


def test_classify_keeps_the_response_digest() -> None:
    result = classify("GET", "configuration", ProviderResponse(200, b"abc"))
    assert result.response_digest == hashlib.sha256(b"abc").hexdigest()


@db
def test_connection_reset_after_a_put_is_uncertain(world: World) -> None:
    world.fake.script.append(Reply(reset=True))
    with pytest.raises(CommandError):
        setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    assert [c.state for c in world.calls()] == [CallState.UNCERTAIN]


@db
def test_a_non_boolean_switch_makes_the_reply_unreadable(world: World) -> None:
    activate(world)
    world.fake.company["habilita_nfe"] = 1
    outcome = sync_company_status(world.reload())
    assert (outcome.state, outcome.provider_code) == (
        CallState.FAILED,
        "resposta_invalida",
    )


@db
def test_drift_stores_the_reported_certificate_cnpj(world: World) -> None:
    activate(world)
    world.fake.company["certificado_cnpj"] = OTHER_CNPJ
    sync_company_status(world.reload())
    link = world.reload()
    assert (link.credential_state, link.certificate_cnpj) == ("divergente", OTHER_CNPJ)


@db
def test_a_company_of_another_cnpj_vouches_for_no_certificate(world: World) -> None:
    activate(world)
    world.fake.company["cnpj"] = OTHER_CNPJ
    sync_company_status(world.reload())
    link = world.reload()
    assert (link.credential_state, link.certificate_cnpj) == ("divergente", "")


@db
def test_requests_carry_basic_auth_and_accept_json(world: World) -> None:
    sync_company_status(world.link)
    headers = {k.lower(): v for k, v in world.fake.requests[0].headers}
    assert headers["accept"] == "application/json"
    assert headers["authorization"].startswith("Basic ")


@db
def test_a_failed_company_read_keeps_the_status_code(world: World) -> None:
    world.fake.script.append(Reply(status=500, body=b"{}"))
    outcome = sync_company_status(world.link)
    assert (outcome.state, outcome.provider_code) == (
        CallState.FAILED,
        "focus_http_500",
    )


@db
def test_dry_run_accepts_any_well_formed_validation_reply(world: World) -> None:
    world.fake.script.append(Reply(status=200, body=b"{}"))
    output = setup(
        world, "--streams", "nfsen_recebidas", "--start-month", "2026-11", "--dry-run"
    )
    assert "Simulação validada" in output
    assert not list(Path(".evidence").glob("AXRM-FOCUS-setup-*.txt"))


@db
def test_an_unfinalized_read_reports_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(call_protocol, "finalize", lambda *_a, **_k: False)
    before = world.reload()
    outcome = sync_company_status(before)
    assert outcome.report is None
    assert outcome.state != CallState.SUCCESS
    assert world.reload().version == before.version


@db
def test_capture_selects_only_focus_links_of_the_configured_environment(
    world: World, settings: SettingsWrapper
) -> None:
    activate(world)
    with tenant_context(world.tenant.pk):
        serpro = ProviderConnection.objects.create(
            tenant=world.tenant, provider="serpro", environment="fake", status="ativa"
        )
        ClientProviderLink.objects.create(
            tenant=world.tenant,
            client=world.client,
            connection=serpro,
            provider_company_ref="serpro-company",
            status="ativa",
            streams=["nfsen_recebidas"],
            capture_start_month=dt.date(2026, 11, 1),
            credential_state="ativa",
        )
    assert [link.pk for link in _capture(world)] == [world.link.pk]
    settings.FOCUS_ENVIRONMENT = "homologacao"
    assert _capture(world) == []


@db
def test_setup_finds_only_an_active_firms_focus_link_in_this_environment(
    world: World,
) -> None:
    # A Focus link of another environment is not this client's link here.
    with tenant_context(world.tenant.pk):
        ProviderConnection.objects.update(environment="homologacao")
    with pytest.raises(CommandError, match="sem vínculo"):
        setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    with tenant_context(world.tenant.pk):
        ProviderConnection.objects.update(environment="fake")
        other = ClientCompany.objects.create(
            tenant=world.tenant, legal_name="Outro", cnpj=OTHER_CNPJ, is_mei=True
        )
        serpro = ProviderConnection.objects.create(
            tenant=world.tenant, provider="serpro", environment="fake", status="ativa"
        )
        ClientProviderLink.objects.create(
            tenant=world.tenant,
            client=other,
            connection=serpro,
            provider_company_ref="serpro-only",
        )
    # A client with only a SERPRO link has no Focus link.
    with pytest.raises(FocusRefused, match="sem vínculo"):
        plan_setup(other.pk, "nfsen_recebidas", "2026-11")
    # An inactive firm's client is never found.
    Tenant.objects.filter(pk=world.tenant.pk).update(is_active=False)
    with pytest.raises(CommandError, match="sem vínculo"):
        setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    assert world.fake.requests == []


@db
def test_nfsen_receipt_names_no_qualification_evidence(
    world: World, settings: SettingsWrapper
) -> None:
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = QUALIFICATION_REF
    setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    receipt = json.loads(
        next(Path(".evidence").glob("AXRM-FOCUS-setup-*.txt")).read_text()
    )
    assert receipt["qualification_ref"] == ""


@db
def test_confirmation_never_revives_a_link_revoked_meanwhile(world: World) -> None:
    plan = plan_setup(world.client.pk, "nfsen_recebidas", "2026-11")
    with tenant_context(world.tenant.pk):
        ClientProviderLink.objects.filter(pk=world.link.pk).update(status="revogada")
    link = focus_company._confirm_setup(plan)
    assert (link.status, link.streams) == ("revogada", ["nfsen_recebidas"])


def _versions(world: World, action: str) -> list[int]:
    with tenant_context(world.tenant.pk):
        return [
            int(meta["version"])
            for meta in Event.objects.filter(action=action).values_list(
                "metadata", flat=True
            )
        ]


@db
def test_confirmed_setup_advances_the_link_version_once(world: World) -> None:
    setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    synced = max(_versions(world, "integrations.focus_company_status_synced"))
    assert _versions(world, "integrations.focus_setup_confirmed") == [synced + 1]
    assert world.reload().version == synced + 1


@db
def test_stopping_one_stream_versions_and_audits_the_link(world: World) -> None:
    activate(world, streams=["nfe_recebidas", "nfsen_recebidas"])
    before = world.reload().version
    disable(world, "--stream", "nfe_recebidas")
    assert _versions(world, "integrations.focus_streams_stopped") == [before + 1]
    with tenant_context(world.tenant.pk):
        assert Event.objects.filter(
            action="integrations.focus_disable_requested"
        ).exists()


@db
def test_capture_reads_need_an_active_link_even_with_authority(world: World) -> None:
    activate(world, status="configuracao_pendente")
    with tenant_context(world.tenant.pk):
        AuthorityRecord.objects.create(
            tenant=world.tenant,
            client=world.client,
            kind="autorizacao_captura",
            service="captura_nfe",
            code="autorizacao_cliente",
            status="ativa",
            valid_from=dt.date(2026, 1, 1),
            source_call_ref="synthetic-declaration",
        )
    with pytest.raises(AdmissionDeniedError):
        reserve(world.tenant.pk, world.link.pk, _capture_request())
    activate(world)
    call = reserve(world.tenant.pk, world.link.pk, _capture_request())
    assert (call.operation, call.operation_class) == ("FC-NFE", "read")
    with tenant_context(world.tenant.pk):
        assert ProviderCall.objects.filter(operation="FC-NFE").count() == 1


@db
def test_homologation_qualification_reads_the_configured_test_company(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real homologation origin, rerouted to the loopback fake for this test.
    monkeypatch.setitem(transport.ORIGINS, ("focus", "homologacao"), world.fake.origin)
    monkeypatch.setenv("FOCUS_SECRET_FOCUS_QUALIFICACAO", "synthetic-homolog-token")
    monkeypatch.setenv("FOCUS_QUALIFICATION_COMPANY_REF", SUBJECT)
    monkeypatch.setenv("FOCUS_QUALIFICATION_CNPJ", CNPJ)
    evidence = qualify_empresa_status("homologacao")
    assert evidence.http_status == 200
    assert evidence.parsed_fields["certificate_cnpj_matches"] is True
    assert [r.path for r in world.fake.requests] == [f"/v2/empresas/{SUBJECT}"]
    monkeypatch.delenv("FOCUS_QUALIFICATION_CNPJ")
    with pytest.raises(RuntimeError, match="empresa_de_qualificacao_ausente"):
        qualify_empresa_status("homologacao")
    monkeypatch.setenv("FOCUS_QUALIFICATION_CNPJ", CNPJ)
    monkeypatch.delenv("FOCUS_QUALIFICATION_COMPANY_REF")
    with pytest.raises(RuntimeError, match="empresa_de_qualificacao_ausente"):
        qualify_empresa_status("homologacao")


# --- Assertion witnesses: exact exit text and call state ------------------------------
#
# A mutant that breaks one of these paths makes a command raise or a call end in
# another state; `outcome_of` turns either into text, so it fails an assertion.


def outcome_of(action: Callable[[], object]) -> str:
    """Return "ok", the action's result, or the exception it raised, as text."""
    try:
        result = action()
    except Exception as error:  # noqa: BLE001 - the witness reports any failure
        return f"{type(error).__name__}: {error}"
    return "ok" if result is None else str(result)


def _quiet(output: str) -> None:
    assert output is not None


def _setup(world: World, *args: str) -> Callable[[], None]:
    return lambda: _quiet(setup(world, *args))


def _disable(world: World, *args: str) -> Callable[[], None]:
    return lambda: _quiet(disable(world, *args))


NFSEN = ("--streams", "nfsen_recebidas", "--start-month", "2026-11")
INVALID_STREAMS = (
    "CommandError: Fluxos inválidos: use nfe_recebidas e/ou nfsen_recebidas."
)


@db
def test_setup_exits_zero_on_the_golden_path(world: World) -> None:
    assert outcome_of(_setup(world, *NFSEN)) == "ok"


@db
def test_setup_exits_zero_with_the_same_fenced_month(world: World) -> None:
    activate(world)
    assert outcome_of(_setup(world, *NFSEN)) == "ok"


@db
def test_dry_run_exits_zero(world: World) -> None:
    assert outcome_of(_setup(world, *NFSEN, "--dry-run")) == "ok"


@db
def test_nfe_setup_with_the_qualification_exits_zero(
    world: World, settings: SettingsWrapper
) -> None:
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = QUALIFICATION_REF
    args = ("--streams", "nfe_recebidas", "--start-month", "2026-11")
    assert outcome_of(_setup(world, *args)) == "ok"


@db
def test_a_dry_run_focus_refuses_exits_non_zero(world: World) -> None:
    world.fake.script.append(Reply(status=422, body=b'{"codigo":"erro_validacao"}'))
    assert outcome_of(_setup(world, *NFSEN, "--dry-run")) == (
        "CommandError: A Focus não validou a configuração simulada."
    )


@db
def test_disable_exits_zero_on_the_whole_link(world: World) -> None:
    activate(world)
    assert outcome_of(_disable(world)) == "ok"


@db
def test_disable_exits_zero_on_one_stream(world: World) -> None:
    activate(world, streams=["nfe_recebidas", "nfsen_recebidas"])
    world.fake.company["habilita_manifestacao_homologacao"] = True
    assert outcome_of(_disable(world, "--stream", "nfe_recebidas")) == "ok"


@db
def test_a_second_disable_after_an_unconfirmed_one_exits_zero(world: World) -> None:
    activate(world)
    world.fake.apply_puts = False
    outcome_of(_disable(world))
    world.fake.apply_puts = True
    assert outcome_of(_disable(world)) == "ok"


@pytest.mark.parametrize("streams", ["", "nfsen_recebidas,nfsen_recebidas"])
@db
def test_invalid_streams_are_a_command_error_with_the_exact_text(
    world: World, streams: str
) -> None:
    result = outcome_of(_setup(world, "--streams", streams, "--start-month", "2026-11"))
    assert result == INVALID_STREAMS


@pytest.mark.parametrize(
    ("change", "state"),
    [
        (
            {
                "certificado_cnpj": None,
                "certificado_valido_de": None,
                "certificado_valido_ate": None,
            },
            CallState.SUCCESS,
        ),
    ],
)
@db
def test_sync_of_a_company_without_certificate_succeeds(
    world: World, change: dict[str, object], state: str
) -> None:
    activate(world)
    world.fake.company.update(change)
    link = world.reload()
    assert outcome_of(lambda: sync_company_status(link).state) == state


@pytest.mark.parametrize("body", [b"not json", b"[]"])
@db
def test_sync_of_an_unreadable_company_fails_definitively(
    world: World, body: bytes
) -> None:
    world.fake.script.append(Reply(status=200, body=body))
    link = world.reload()
    assert outcome_of(lambda: sync_company_status(link).state) == CallState.FAILED


@db
def test_sync_stores_the_switch_snapshot(world: World) -> None:
    sync_company_status(world.link)
    flags = world.reload().provider_flags
    assert "habilita_nfsen_recebidas_homologacao" in flags
    assert "certificado_especifico" in flags


def test_each_send_starts_without_the_previous_reply() -> None:
    request = company_request(SUBJECT)
    adapter = _adapter(request)
    adapter.report = cast("focus_adapter.CompanyReport", object())
    adapter.response = ProviderResponse(200, b"{}")
    other = update_request(SUBJECT, {"certificado_especifico": True})
    adapter.send(_dispatch(other, "FC-EMP", "configuration"))
    assert _reply(adapter) == (None, None)


def _reply(adapter: FocusAdapter) -> tuple[object, object]:
    return adapter.report, adapter.response


@db
def test_qualification_leaves_its_synthetic_firm_inactive(world: World) -> None:
    before = set(Tenant.objects.values_list("pk", flat=True))
    qualify_empresa_status("fake")
    created = Tenant.objects.exclude(pk__in=before)
    assert created.count() == 1
    assert not created.filter(is_active=True).exists()


@db
def test_qualification_without_a_cnpj_refuses_with_the_exact_text(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOCUS_QUALIFICATION_COMPANY_REF", SUBJECT)
    monkeypatch.delenv("FOCUS_QUALIFICATION_CNPJ", raising=False)
    assert outcome_of(lambda: qualify_empresa_status("homologacao")) == (
        "RuntimeError: empresa_de_qualificacao_ausente"
    )


# --- Assertion witnesses, round 2 (crash-only and surviving mutants of mutB) ----------


def test_an_allowlist_refusal_in_send_reports_its_code() -> None:
    request = ProviderRequest(
        variant="update", subject=SUBJECT, body=b'{"senha_certificado":"x"}'
    )
    adapter = _adapter(request)
    dispatch = _dispatch(request, "FC-EMP", "configuration")
    assert outcome_of(lambda: adapter.send(dispatch).provider_code) == (
        "operacao_nao_permitida"
    )


def test_parsing_outcomes_as_text(settings: SettingsWrapper) -> None:
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = QUALIFICATION_REF
    assert outcome_of(lambda: _reported_day("")) == "ok"
    body = json.dumps({"id": COMPANY_ID, "habilita_nfe": 1}).encode()
    assert outcome_of(lambda: parse_company(ProviderResponse(200, body), SUBJECT)) == (
        "ValueError: "
    )
    production = json.dumps({"habilita_manifestacao": True}).encode()
    assert (
        outcome_of(
            lambda: resolve(
                "focus",
                "FC-EMP",
                ProviderRequest(variant="update", subject=SUBJECT, body=production),
            )[1]
        )
        == f"/v2/empresas/{SUBJECT}"
    )


def test_limiter_windows_are_per_token_name() -> None:
    first = ProviderConnection(environment="homologacao", secret_ref=NAME_A)
    second = ProviderConnection(environment="homologacao", secret_ref=NAME_B)
    assert limiter_key(first) != limiter_key(second)
    assert limiter_key(first) == f"homologacao:{NAME_A}"


def test_an_expired_window_frees_promptly() -> None:
    # A bounded wait on the exact completion event; the pristine call is instant.
    class Clock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    limiter = TokenRateLimiter(limit=1, clock=clock)
    assert limiter.acquire("k") is True
    clock.now = 61.0
    done = threading.Event()
    result: list[bool] = []

    def run() -> None:
        result.append(limiter.acquire("k"))
        done.set()

    threading.Thread(target=run, daemon=True).start()
    assert done.wait(timeout=5), "acquire did not return"
    assert result == [True]


@db
def test_sync_outcomes_as_text(world: World) -> None:
    world.fake.script.append(Reply(status=200, body=b"x" * (5 * 1024 * 1024 + 1)))
    assert outcome_of(lambda: sync_company_status(world.link).state) == (
        CallState.FAILED
    )


@db
def test_a_non_boolean_switch_fails_the_sync_as_text(world: World) -> None:
    world.fake.company["habilita_nfe"] = 1
    assert outcome_of(lambda: sync_company_status(world.link).state) == (
        CallState.FAILED
    )


@db
def test_a_lost_claim_ends_as_claimed_as_text(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lost(*_args: object, **_kwargs: object) -> None:
        raise StaleClaimError

    monkeypatch.setattr(focus_company, "perform", lost)
    assert outcome_of(lambda: sync_company_status(world.link).state) == (
        CallState.CLAIMED
    )


@db
def test_the_accept_header_is_json(world: World) -> None:
    sync_company_status(world.link)
    headers = {k.lower(): v for k, v in world.fake.requests[0].headers}
    assert headers.get("accept") == "application/json"


@db
def test_a_tolerant_dry_run_reply_exits_zero(world: World) -> None:
    world.fake.script.append(Reply(status=200, body=b"{}"))
    assert outcome_of(_setup(world, *NFSEN, "--dry-run")) == "ok"


@db
def test_an_invalid_calendar_month_is_a_command_error(world: World) -> None:
    args = ("--streams", "nfsen_recebidas", "--start-month", "2026-13")
    assert outcome_of(_setup(world, *args)) == (
        "CommandError: Mês inválido: '2026-13'; use AAAA-MM."
    )


@db
def test_an_uncertain_put_names_every_unconfirmed_field(world: World) -> None:
    world.fake.script.append(Reply(status=503, body=b"{}"))
    assert outcome_of(_setup(world, *NFSEN)) == (
        "CommandError: Configuração não confirmada pela leitura de volta: "
        "certificado_especifico, data_inicio_recebimento_nfsen, "
        "habilita_filtro_mde_destinatario_empresa, "
        "habilita_nfsen_recebidas_homologacao."
    )


@db
def test_disabling_a_never_configured_link_exits_zero(world: World) -> None:
    assert outcome_of(_disable(world)) == "ok"


@db
def test_disabling_an_unknown_stream_is_the_exact_command_error(world: World) -> None:
    activate(world)
    assert outcome_of(_disable(world, "--stream", "nfse_emitidas")) == INVALID_STREAMS


@db
def test_admission_refusals_are_exact_command_errors(
    world: World, settings: SettingsWrapper
) -> None:
    settings.FOCUS_PAUSED = True
    paused = "CommandError: Chamada recusada pela admissão (pausada); nada foi enviado."
    assert outcome_of(_setup(world, *NFSEN)) == paused
    activate(world)
    assert outcome_of(_disable(world)) == paused
    settings.FOCUS_PAUSED = False
    with tenant_context(world.tenant.pk):
        ProviderBudget.objects.all().delete()
    assert outcome_of(_disable(world)) == (
        "CommandError: Sem orçamento Focus para o mês (cota_ausente); nada foi enviado."
    )


@db
def test_qualification_without_a_company_refuses_with_the_exact_text(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOCUS_QUALIFICATION_CNPJ", CNPJ)
    monkeypatch.delenv("FOCUS_QUALIFICATION_COMPANY_REF", raising=False)
    assert outcome_of(lambda: qualify_empresa_status("homologacao")) == (
        "RuntimeError: empresa_de_qualificacao_ausente"
    )


@db
def test_status_is_judged_on_the_injected_day(world: World) -> None:
    activate(world)
    link = world.reload()
    assert link.certificate_valid_to is not None
    sync_company_status(link, today=link.certificate_valid_to + dt.timedelta(days=1))
    assert world.reload().credential_state == "vencida"


@db
def test_capture_authority_is_checked_on_the_exact_link(world: World) -> None:
    # Given the client's capture authority and a second, divergent Focus link.
    activate(world)
    with tenant_context(world.tenant.pk):
        AuthorityRecord.objects.create(
            tenant=world.tenant,
            client=world.client,
            kind="autorizacao_captura",
            service="captura_nfe",
            code="autorizacao_cliente",
            status="ativa",
            valid_from=dt.date(2026, 1, 1),
            source_call_ref="synthetic-declaration",
        )
        other = ProviderConnection.objects.create(
            tenant=world.tenant,
            provider="focus",
            environment="homologacao",
            status="ativa",
        )
        ClientProviderLink.objects.create(
            tenant=world.tenant,
            client=world.client,
            connection=other,
            provider_company_ref="9999",
            status="ativa",
            credential_state="divergente",
        )
    # When a capture read is reserved on the healthy link, it is admitted.
    assert (
        outcome_of(
            lambda: reserve(world.tenant.pk, world.link.pk, _capture_request()).state
        )
        == CallState.RESERVED
    )


# --- R11: the limiter's lock, under real concurrency ----------------------------------


class _HeldWindow(deque[float]):
    """Hold every caller inside the limiter's check-then-append, unless locked.

    With the lock held (the shipped code) nothing waits. Without it, every thread
    waits at `gate` inside the length check, so all of them see the same empty
    window before any appends: the race is forced, not left to scheduling luck.
    """

    def __init__(self, held: Callable[[], bool], gate: threading.Barrier) -> None:
        super().__init__()
        self.held = held
        self.gate = gate

    def __len__(self) -> int:
        # Read first, then wait: every unlocked caller acts on the same stale length.
        size = super().__len__()
        if not self.held():
            self.gate.wait(timeout=10)
        return size


def test_the_limiter_admits_exactly_its_limit_under_concurrent_callers() -> None:
    callers = 100
    limiter = TokenRateLimiter(clock=lambda: 1000.0)
    limiter._sent["k"] = _HeldWindow(limiter._lock.locked, threading.Barrier(callers))
    start = threading.Barrier(callers)
    results: list[bool] = []
    record = threading.Lock()

    def call() -> None:
        start.wait(timeout=10)
        admitted = limiter.acquire("k")
        with record:
            results.append(admitted)

    threads = [threading.Thread(target=call, daemon=True) for _ in range(callers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not [thread for thread in threads if thread.is_alive()]
    assert len(results) == callers
    assert results.count(True) == 60


# --- The PQ-FOCUS-1 gate at the wire (allowlist) -----------------------------------

REFUSED = "OperacaoNaoPermitidaError: operacao_nao_permitida"


def _company_update(fields: dict[str, object]) -> Callable[[], object]:
    body = json.dumps(fields).encode()
    request = ProviderRequest(variant="update", subject=SUBJECT, body=body)
    return lambda: resolve("focus", "FC-EMP", request)[1]


@pytest.mark.parametrize(
    "switch", ["habilita_manifestacao", "habilita_manifestacao_homologacao"]
)
def test_turning_received_nfe_on_needs_the_pq_focus_1_id_at_the_wire(
    settings: SettingsWrapper, switch: str
) -> None:
    path = f"/v2/empresas/{SUBJECT}"
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = ""
    # Off is always allowed, so capture can always be stopped.
    assert outcome_of(_company_update({switch: False})) == path
    # On (or any value that is not false) is refused without the evidence id.
    assert outcome_of(_company_update({switch: True})) == REFUSED
    assert outcome_of(_company_update({switch: 1})) == REFUSED
    for value in ("AXRM-PQ-FOCUS-1 extra", "xAXRM-PQ-FOCUS-1", "PQ-FOCUS-1"):
        settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = value
        assert outcome_of(_company_update({switch: True})) == REFUSED, value
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = None
    assert outcome_of(_company_update({switch: True})) == REFUSED
    # With the recorded evidence id the switch may be turned on.
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = QUALIFICATION_REF
    assert outcome_of(_company_update({switch: True})) == path


def test_other_receipt_switches_do_not_need_the_pq_focus_1_id(
    settings: SettingsWrapper,
) -> None:
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = ""
    fields: dict[str, object] = {
        "habilita_nfsen_recebidas_homologacao": True,
        "certificado_especifico": True,
    }
    assert outcome_of(_company_update(fields)) == f"/v2/empresas/{SUBJECT}"
