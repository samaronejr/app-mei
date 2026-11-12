"""AXRM-019: the Focus adapter, company status sync and the audited operator commands.

Every test drives the real management command or service against the stateful
synthetic Focus company (`tests/fakes/focus.py`); no test reaches a real provider.
"""

import datetime as dt
import importlib
import inspect
import io
import json
import pkgutil
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

import apps.integrations.focus as focus_package
from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.integrations import provider_journal
from apps.integrations.allowlist import resolve
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.budget_pricing import FOCUS_PLAN
from apps.integrations.call_contracts import AdmissionDeniedError, CallRequest
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_protocol import reserve
from apps.integrations.focus import adapter as focus_adapter
from apps.integrations.focus.adapter import (
    ISSUANCE_FLAGS,
    FocusAdapter,
    update_request,
)
from apps.integrations.focus.company import (
    NFE_QUALIFICATION_REFUSAL,
    capture_links,
    setup_fields,
    sync_company_status,
)
from apps.integrations.focus.rate_limit import TokenRateLimiter
from apps.integrations.models import (
    ClientProviderLink,
    IntegrationIssue,
    ProviderConnection,
)
from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import OperacaoNaoPermitida, ProviderRequest
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.fakes.focus import FakeFocusCompany, synthetic_company
from tests.fakes.provider import Reply

pytestmark = pytest.mark.django_db(transaction=True)

CNPJ = "11222333000181"
OTHER_CNPJ = "12345678000195"
COMPANY_ID = 4321
QUALIFICATION_REF = "AXRM-PQ-FOCUS-1-2026-11-05"
MASTER_REF = "focus/token_master"
TOKENS = ("SYNTHETICPRODUCTIONTOKEN0000000000", "SYNTHETICHOMOLOGATIONTOKEN00000000")


@dataclass
class World:
    tenant: Tenant
    client: ClientCompany
    link: ClientProviderLink
    fake: FakeFocusCompany

    def reload(self) -> ClientProviderLink:
        with tenant_context(self.tenant.pk):
            return ClientProviderLink.objects.get(pk=self.link.pk)

    def calls(self) -> list[ProviderCall]:
        with tenant_context(self.tenant.pk):
            return list(ProviderCall.objects.order_by("created_at", "pk"))

    def issues(self) -> set[str]:
        with tenant_context(self.tenant.pk):
            return set(
                IntegrationIssue.objects.filter(client=self.client).values_list(
                    "reason", flat=True
                )
            )


def _iso(day: dt.date) -> str:
    return f"{day.isoformat()}T09:00:00-03:00"


@pytest.fixture
def world(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[World]:
    settings.PROVIDERS_PAUSED = False
    settings.FOCUS_PAUSED = False
    settings.FOCUS_ENVIRONMENT = "fake"
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.FOCUS_CAPTURE_PILOT_START_MONTH = "2026-11"
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = ""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(focus_adapter, "LIMITER", TokenRateLimiter())
    tenant = Tenant.objects.create(
        name="Focus sintetico",
        slug="focus-sintetico",
        features=["cycles_r1", "scan_required", "document_capture"],
    )
    owner = User.objects.create_user(email="focus-owner@example.test")
    Membership.objects.create(tenant=tenant, user=owner, role=TenantRole.OWNER)
    with tenant_context(tenant.pk):
        client = ClientCompany.objects.create(
            tenant=tenant, legal_name="Cliente sintetico", cnpj=CNPJ, is_mei=True
        )
        provider = ProviderConnection.objects.create(
            tenant=tenant,
            provider="focus",
            environment="fake",
            status="ativa",
            secret_ref=MASTER_REF,
        )
        link = ClientProviderLink.objects.create(
            tenant=tenant,
            client=client,
            connection=provider,
            provider_company_ref=str(COMPANY_ID),
        )
        ProviderBudget.objects.create(
            tenant=tenant,
            provider="focus",
            window_start=timezone.now().date().replace(day=1),
            cap_microbrl=5_000_000,
            tariff_revision=FOCUS_PLAN,
        )
    with FakeFocusCompany(synthetic_company(COMPANY_ID, CNPJ)) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        yield World(tenant, client, link, fake)


def setup(world: World, *args: str) -> str:
    out = io.StringIO()
    call_command(
        "focus_company_setup", "--client", str(world.client.pk), *args, stdout=out
    )
    return out.getvalue()


def disable(world: World, *args: str) -> str:
    out = io.StringIO()
    call_command(
        "focus_company_disable", "--client", str(world.client.pk), *args, stdout=out
    )
    return out.getvalue()


def activate(world: World, **fields: object) -> None:
    """A link that setup already confirmed, with an accepted certificate."""
    today = timezone.localdate()
    values: dict[str, object] = {
        "status": "ativa",
        "disablement_state": "ativa",
        "streams": ["nfsen_recebidas"],
        "capture_start_month": dt.date(2026, 11, 1),
        "credential_state": "ativa",
        "credential_generation": 1,
        "certificate_cnpj": CNPJ,
        "certificate_valid_from": today - dt.timedelta(days=100),
        "certificate_valid_to": today + dt.timedelta(days=200),
    }
    values.update(fields)
    with tenant_context(world.tenant.pk):
        ClientProviderLink.objects.filter(pk=world.link.pk).update(**values)
    world.fake.company.update(
        certificado_valido_de=_iso(today - dt.timedelta(days=100)),
        certificado_valido_ate=_iso(today + dt.timedelta(days=200)),
        habilita_nfsen_recebidas_homologacao=True,
        habilita_filtro_mde_destinatario_empresa=True,
        certificado_especifico=True,
        data_inicio_recebimento_nfsen="2026-11-01",
    )


# --- Setup: golden requests and refusals --------------------------------------


def test_setup_sends_exactly_the_allowed_fields_and_confirms_by_read_back(
    world: World,
) -> None:
    # Given an unconfigured link and an operator asking for NFS-e Nacional capture.
    # When the real command runs.
    output = setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    # Then the one PUT is the golden request: these four fields, nothing else.
    puts = [r for r in world.fake.requests if r.method == "PUT"]
    assert [(r.method, r.path) for r in world.fake.requests] == [
        ("PUT", f"/v2/empresas/{COMPANY_ID}"),
        ("GET", f"/v2/empresas/{COMPANY_ID}"),
    ]
    assert json.loads(puts[0].body) == {
        "certificado_especifico": True,
        "data_inicio_recebimento_nfsen": "2026-11-01",
        "habilita_filtro_mde_destinatario_empresa": True,
        "habilita_nfsen_recebidas_homologacao": True,
    }
    # And the read-back confirmed it: the link is active, scoped and dated.
    link = world.reload()
    assert link.status == "ativa"
    assert link.streams == ["nfsen_recebidas"]
    assert link.capture_start_month == dt.date(2026, 11, 1)
    assert link.provider_flags["habilita_nfsen_recebidas_homologacao"] is True
    # And both calls are journaled, included-plan configuration calls.
    calls = world.calls()
    assert [
        (c.operation, c.operation_class, c.billing_class, c.state) for c in calls
    ] == [
        ("FC-EMP", "configuration", "incluido_no_plano", CallState.SUCCESS),
        ("FC-EMP", "configuration", "incluido_no_plano", CallState.SUCCESS),
    ]
    assert len(provider_journal.read(world.tenant.pk)) == 4
    assert "confirmada" in output


def test_setup_receipt_lists_changed_fields_without_tokens(world: World) -> None:
    # Given a confirmed setup.
    setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    # When the operator reads the .evidence receipt the command wrote.
    receipts = list(Path(".evidence").glob("AXRM-FOCUS-setup-*.txt"))
    assert len(receipts) == 1
    raw = receipts[0].read_text()
    receipt = json.loads(raw)
    # Then it names the changed fields and never a token or the authorization.
    assert receipt["fields"] == {
        "certificado_especifico": True,
        "data_inicio_recebimento_nfsen": "2026-11-01",
        "habilita_filtro_mde_destinatario_empresa": True,
        "habilita_nfsen_recebidas_homologacao": True,
    }
    assert receipt["confirmed"] is True
    for secret in (*TOKENS, "synthetic-focus", "Basic "):
        assert secret not in raw


def test_setup_refuses_nfe_without_the_qualification_evidence(world: World) -> None:
    # Given no PQ-FOCUS-1 evidence id recorded in the platform setting.
    # When the operator asks for received NF-e capture.
    with pytest.raises(CommandError) as refusal:
        setup(
            world,
            "--streams",
            "nfe_recebidas,nfsen_recebidas",
            "--start-month",
            "2026-11",
        )
    # Then the exact pt-BR refusal, and no request (so no habilita_manifestacao*).
    assert str(refusal.value) == NFE_QUALIFICATION_REFUSAL
    assert NFE_QUALIFICATION_REFUSAL == (
        "Captura de NF-e recebidas bloqueada: falta a evidencia PQ-FOCUS-1"
    )
    assert world.fake.requests == []
    assert world.calls() == []
    assert world.reload().capture_start_month is None


@pytest.mark.parametrize("value", ["sim", "PQ-FOCUS-1", "AXRM-PQ-FOCUS-2-x", " "])
def test_setup_refuses_nfe_when_the_setting_is_not_a_pq_focus_1_id(
    world: World, settings: SettingsWrapper, value: str
) -> None:
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = value
    with pytest.raises(CommandError, match="PQ-FOCUS-1"):
        setup(world, "--streams", "nfe_recebidas", "--start-month", "2026-11")
    assert world.fake.requests == []


def test_nfsen_only_setup_never_sends_a_manifestacao_key(world: World) -> None:
    setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    for body in world.fake.bodies():
        assert not [key for key in body if key.startswith("habilita_manifestacao")]


def test_nfe_setup_with_qualification_sends_only_the_environment_key(
    world: World, settings: SettingsWrapper
) -> None:
    # Given the recorded PQ-FOCUS-1 evidence id.
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = QUALIFICATION_REF
    # When the operator configures received NF-e capture outside production.
    setup(world, "--streams", "nfe_recebidas", "--start-month", "2026-11")
    # Then the homologation key alone is sent, never the production one.
    assert world.fake.bodies() == [
        {
            "certificado_especifico": True,
            "data_inicio_recebimento_nfe": "2026-11-01",
            "habilita_filtro_mde_destinatario_empresa": True,
            "habilita_manifestacao_homologacao": True,
        }
    ]
    assert world.reload().streams == ["nfe_recebidas"]


def test_production_fields_use_only_the_production_keys() -> None:
    fields = setup_fields(
        ["nfe_recebidas", "nfsen_recebidas"], dt.date(2026, 11, 1), "producao"
    )
    assert fields == {
        "certificado_especifico": True,
        "data_inicio_recebimento_nfe": "2026-11-01",
        "data_inicio_recebimento_nfsen": "2026-11-01",
        "habilita_filtro_mde_destinatario_empresa": True,
        "habilita_manifestacao": True,
        "habilita_nfsen_recebidas_producao": True,
    }
    homologation = setup_fields(
        ["nfsen_recebidas"], dt.date(2026, 12, 1), "homologacao"
    )
    assert homologation["habilita_nfsen_recebidas_homologacao"] is True
    assert "habilita_nfsen_recebidas_producao" not in homologation
    assert not set(fields) & ISSUANCE_FLAGS


def test_setup_refuses_a_different_capture_start_month(world: World) -> None:
    # Given a link whose capture already starts in November 2026.
    with tenant_context(world.tenant.pk):
        ClientProviderLink.objects.filter(pk=world.link.pk).update(
            capture_start_month=dt.date(2026, 11, 1)
        )
    # When the operator asks for December.
    with pytest.raises(CommandError, match="2026-11"):
        setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-12")
    # Then nothing is sent and the month is unchanged.
    assert world.fake.requests == []
    assert world.reload().capture_start_month == dt.date(2026, 11, 1)


def test_setup_accepts_the_same_capture_start_month_again(world: World) -> None:
    with tenant_context(world.tenant.pk):
        ClientProviderLink.objects.filter(pk=world.link.pk).update(
            capture_start_month=dt.date(2026, 11, 1)
        )
    setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    assert world.reload().status == "ativa"


@pytest.mark.parametrize("month", ["2026-10", "2025-12"])
def test_setup_refuses_a_start_before_the_pilot_month(world: World, month: str) -> None:
    with pytest.raises(CommandError, match="piloto"):
        setup(world, "--streams", "nfsen_recebidas", "--start-month", month)
    assert world.fake.requests == []
    assert world.reload().capture_start_month is None


def test_setup_refuses_without_a_configured_pilot_month(
    world: World, settings: SettingsWrapper
) -> None:
    settings.FOCUS_CAPTURE_PILOT_START_MONTH = ""
    with pytest.raises(CommandError, match="piloto"):
        setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    assert world.fake.requests == []


@pytest.mark.parametrize(
    ("streams", "month"),
    [
        ("nfse_emitidas", "2026-11"),
        ("nfsen_recebidas,nfsen_recebidas", "2026-11"),
        ("", "2026-11"),
        ("nfsen_recebidas", "2026-13"),
        ("nfsen_recebidas", "2026-11-01"),
    ],
)
def test_setup_refuses_malformed_streams_and_months(
    world: World, streams: str, month: str
) -> None:
    with pytest.raises(CommandError):
        setup(world, "--streams", streams, "--start-month", month)
    assert world.fake.requests == []


def test_setup_refuses_a_link_being_disabled(world: World) -> None:
    activate(world, disablement_state="pausada_localmente")
    with pytest.raises(CommandError):
        setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    assert world.fake.requests == []


def test_setup_refuses_an_unknown_client(world: World) -> None:
    out = io.StringIO()
    with pytest.raises(CommandError):
        call_command(
            "focus_company_setup",
            "--client",
            "01a1cd9d-0000-7000-8000-000000000000",
            "--streams",
            "nfsen_recebidas",
            "--start-month",
            "2026-11",
            stdout=out,
        )
    assert world.fake.requests == []


def test_issuance_flags_and_key_material_are_refused_before_the_wire() -> None:
    # Given every issuance flag and every certificate field Focus would accept.
    forbidden = [
        *sorted(ISSUANCE_FLAGS),
        "arquivo_certificado_base64",
        "senha_certificado",
        "senha_responsavel",
    ]
    expected = {
        "habilita_nfe",
        "habilita_nfse",
        "habilita_nfsen_producao",
        "habilita_nfce",
        "habilita_cte",
        "habilita_mdfe",
    }
    assert expected == ISSUANCE_FLAGS
    for name in forbidden:
        # When the update builder or the allowlist sees one of them.
        with pytest.raises(OperacaoNaoPermitida):
            update_request(str(COMPANY_ID), {name: True})
        body = json.dumps({name: True}).encode()
        # Then neither lets the request reach a transport.
        with pytest.raises(OperacaoNaoPermitida):
            resolve(
                "focus",
                "FC-EMP",
                ProviderRequest(variant="update", subject=str(COMPANY_ID), body=body),
            )


def test_dry_run_prints_the_plan_and_changes_nothing(world: World) -> None:
    # Given an unconfigured link.
    # When the operator runs the dry run.
    output = setup(
        world, "--streams", "nfsen_recebidas", "--start-month", "2026-11", "--dry-run"
    )
    # Then the planned field set is printed and Focus only validated it.
    plan = json.loads(output.splitlines()[0])
    assert plan["fields"] == {
        "certificado_especifico": True,
        "data_inicio_recebimento_nfsen": "2026-11-01",
        "habilita_filtro_mde_destinatario_empresa": True,
        "habilita_nfsen_recebidas_homologacao": True,
    }
    assert [(r.method, r.path) for r in world.fake.requests] == [
        ("PUT", f"/v2/empresas/{COMPANY_ID}?dry_run=1")
    ]
    assert world.fake.company["habilita_nfsen_recebidas_homologacao"] is False
    link = world.reload()
    assert (link.status, link.streams, link.capture_start_month) == (
        "configuracao_pendente",
        [],
        None,
    )
    assert [c.state for c in world.calls()] == [CallState.SUCCESS]


def test_setup_without_read_back_confirmation_keeps_the_link_pending(
    world: World,
) -> None:
    # Given Focus acknowledges the PUT but does not apply it.
    world.fake.apply_puts = False
    # When the operator runs setup.
    with pytest.raises(CommandError, match="habilita_nfsen_recebidas_homologacao"):
        setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    # Then the link is not activated; the start month stays fenced.
    link = world.reload()
    assert link.status == "configuracao_pendente"
    assert link.streams == []
    assert link.capture_start_month == dt.date(2026, 11, 1)


def test_setup_with_an_uncertain_put_keeps_the_link_pending(world: World) -> None:
    # Given Focus fails after receiving the configuration.
    world.fake.script.append(Reply(status=503, body=b"{}"))
    with pytest.raises(CommandError):
        setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    # Then the call is uncertain owned work, no read-back confirmed anything.
    assert [r.method for r in world.fake.requests] == ["PUT"]
    assert [c.state for c in world.calls()] == [CallState.UNCERTAIN]
    assert world.reload().status == "configuracao_pendente"
    assert "chamada_provedor_incerta" in world.issues()


def test_setup_refused_by_focus_is_failed_not_uncertain(world: World) -> None:
    world.fake.script.append(Reply(status=422, body=b'{"codigo":"erro_validacao"}'))
    with pytest.raises(CommandError):
        setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    assert [c.state for c in world.calls()] == [CallState.FAILED]
    assert world.issues() == set()


# --- Disable with read-back ---------------------------------------------------


def test_disable_sets_receipt_flags_false_and_confirms_by_read_back(
    world: World,
) -> None:
    # Given a capturing link.
    activate(world)
    assert [link.pk for link in _capture(world)] == [world.link.pk]
    # When the operator disables it.
    output = disable(world)
    # Then one PUT turns the receipt flag off, and a GET confirmed it.
    assert world.fake.bodies() == [{"habilita_nfsen_recebidas_homologacao": False}]
    assert [r.method for r in world.fake.requests] == ["PUT", "GET"]
    link = world.reload()
    assert link.disablement_state == "desativada_no_provedor_confirmada"
    assert "desativada_no_provedor_confirmada" in output
    # And the capture sweep's selection skips it.
    assert _capture(world) == []
    assert [c.operation_class for c in world.calls()] == ["configuration"] * 2


def test_disable_without_read_back_confirmation_stays_pending(world: World) -> None:
    # Given Focus acknowledges but does not apply the disable.
    activate(world)
    world.fake.apply_puts = False
    # When the operator disables.
    with pytest.raises(CommandError):
        disable(world)
    # Then the provider step is pending, and capture is already skipped.
    assert world.reload().disablement_state == "desativacao_no_provedor_pendente"
    assert _capture(world) == []
    # When a later run is confirmed by Focus.
    world.fake.apply_puts = True
    disable(world)
    # Then the same machine completes, with a new journaled call per attempt.
    assert world.reload().disablement_state == "desativada_no_provedor_confirmada"
    assert len(world.calls()) == 4


def test_disable_read_back_missing_the_flag_is_not_a_confirmation(
    world: World,
) -> None:
    activate(world)
    del world.fake.company["habilita_nfsen_recebidas_homologacao"]
    world.fake.apply_puts = False
    with pytest.raises(CommandError):
        disable(world)
    assert world.reload().disablement_state == "desativacao_no_provedor_pendente"


def test_disable_works_for_an_expired_or_divergent_certificate(world: World) -> None:
    # Given capture blocked by its certificate state.
    activate(world, credential_state="divergente")
    # When the operator disables the provider side.
    disable(world)
    # Then configuration calls are still admitted.
    assert world.reload().disablement_state == "desativada_no_provedor_confirmada"


def test_disable_of_one_stream_keeps_the_other(
    world: World, settings: SettingsWrapper
) -> None:
    activate(world, streams=["nfe_recebidas", "nfsen_recebidas"])
    world.fake.company["habilita_manifestacao_homologacao"] = True
    disable(world, "--stream", "nfe_recebidas")
    assert world.fake.bodies() == [{"habilita_manifestacao_homologacao": False}]
    link = world.reload()
    assert link.streams == ["nfsen_recebidas"]
    assert link.disablement_state == "ativa"
    assert world.fake.company["habilita_nfsen_recebidas_homologacao"] is True


def test_disable_of_a_confirmed_link_sends_nothing(world: World) -> None:
    activate(world, disablement_state="desativada_no_provedor_confirmada")
    output = disable(world)
    assert world.fake.requests == []
    assert "desativada_no_provedor_confirmada" in output


def test_disable_refuses_an_unknown_stream(world: World) -> None:
    activate(world)
    with pytest.raises(CommandError):
        disable(world, "--stream", "nfse_emitidas")
    assert world.fake.requests == []


# --- Status sync, A7.3 credential states and openers ---------------------------


def test_first_sync_stores_validity_and_flags_without_any_token(world: World) -> None:
    # Given a link without any certificate status yet.
    today = timezone.localdate()
    world.fake.company.update(
        certificado_valido_de=_iso(today - dt.timedelta(days=10)),
        certificado_valido_ate=_iso(today + dt.timedelta(days=300)),
    )
    # When status is synced.
    outcome = sync_company_status(world.link)
    # Then validity and a flags snapshot are stored, never a token.
    assert outcome.state == CallState.SUCCESS
    link = world.reload()
    assert link.certificate_cnpj == CNPJ
    assert link.certificate_valid_from == today - dt.timedelta(days=10)
    assert link.certificate_valid_to == today + dt.timedelta(days=300)
    assert link.certificate_synced_at is not None
    assert (link.credential_state, link.credential_generation) == (
        "confirmacao_pendente",
        1,
    )
    assert link.provider_flags["habilita_nfsen_recebidas_homologacao"] is False
    assert not [key for key in link.provider_flags if "token" in key or "senha" in key]
    # And the second, unchanged report confirms it.
    sync_company_status(world.reload())
    assert (world.reload().credential_state, world.reload().credential_generation) == (
        "ativa",
        1,
    )


def test_tokens_never_reach_the_database_or_the_journal(world: World) -> None:
    sync_company_status(world.link)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT row_to_json(l)::text FROM integrations_clientproviderlink l"
        )
        rows = [row[0] for row in cursor.fetchall()]
        cursor.execute("SELECT row_to_json(c)::text FROM integrations_providercall c")
        rows += [row[0] for row in cursor.fetchall()]
    journal = "".join(p.read_text() for p in Path("media").rglob("*") if p.is_file())
    assert journal
    for secret in TOKENS:
        assert secret not in "".join(rows) + journal


def test_expiring_certificate_is_vencendo_with_owned_work(world: World) -> None:
    activate(world)
    today = timezone.localdate()
    world.fake.company["certificado_valido_ate"] = _iso(today + dt.timedelta(days=30))
    world.fake.company["certificado_valido_de"] = _iso(today - dt.timedelta(days=100))
    with tenant_context(world.tenant.pk):
        ClientProviderLink.objects.filter(pk=world.link.pk).update(
            certificate_valid_to=today + dt.timedelta(days=30)
        )
    sync_company_status(world.reload())
    assert world.reload().credential_state == "vencendo"
    assert world.issues() == {"certificado_provedor_vencendo"}


def test_certificate_31_days_out_stays_active(world: World) -> None:
    activate(world)
    today = timezone.localdate()
    world.fake.company["certificado_valido_ate"] = _iso(today + dt.timedelta(days=31))
    world.fake.company["certificado_valido_de"] = _iso(today - dt.timedelta(days=100))
    with tenant_context(world.tenant.pk):
        ClientProviderLink.objects.filter(pk=world.link.pk).update(
            certificate_valid_to=today + dt.timedelta(days=31)
        )
    sync_company_status(world.reload())
    assert world.reload().credential_state == "ativa"
    assert world.issues() == set()


def test_expired_certificate_is_vencida_and_capture_skips_the_client(
    world: World,
) -> None:
    # Given a capturing link whose certificate Focus reports expired yesterday.
    activate(world)
    yesterday = timezone.localdate() - dt.timedelta(days=1)
    world.fake.company["certificado_valido_ate"] = _iso(yesterday)
    with tenant_context(world.tenant.pk):
        ClientProviderLink.objects.filter(pk=world.link.pk).update(
            certificate_valid_to=yesterday
        )
    # When status is synced.
    sync_company_status(world.reload())
    # Then the credential is vencida, owned, and the capture sweep skips it.
    assert world.reload().credential_state == "vencida"
    assert world.issues() == {"certificado_provedor_vencido"}
    assert _capture(world) == []


def test_drift_sets_divergente_and_blocks_dispatch(world: World) -> None:
    # Given a capturing link and Focus now reporting another CNPJ's certificate.
    activate(world)
    world.fake.company["certificado_cnpj"] = OTHER_CNPJ
    # When status is synced.
    sync_company_status(world.reload())
    # Then the link is divergent and owned.
    link = world.reload()
    assert link.credential_state == "divergente"
    assert world.issues() == {"credencial_divergente"}
    assert _capture(world) == []
    # And a capture dispatch is refused before any wire request or call row.
    before = len(world.fake.requests)
    with pytest.raises(AdmissionDeniedError):
        reserve(world.tenant.pk, world.link.pk, _capture_request())
    assert len(world.fake.requests) == before
    assert {c.operation for c in world.calls()} == {"FC-EMP"}


def test_unrequested_new_certificate_is_drift(world: World) -> None:
    # Given an active certificate and Focus reporting a different validity window.
    activate(world)
    today = timezone.localdate()
    world.fake.company["certificado_valido_de"] = _iso(today - dt.timedelta(days=1))
    world.fake.company["certificado_valido_ate"] = _iso(today + dt.timedelta(days=364))
    sync_company_status(world.reload())
    link = world.reload()
    assert link.credential_state == "divergente"
    assert link.credential_generation == 1


def test_requested_rotation_increments_the_generation(world: World) -> None:
    activate(world, credential_state="rotacao_pendente")
    today = timezone.localdate()
    world.fake.company["certificado_valido_de"] = _iso(today - dt.timedelta(days=1))
    world.fake.company["certificado_valido_ate"] = _iso(today + dt.timedelta(days=364))
    sync_company_status(world.reload())
    link = world.reload()
    assert (link.credential_state, link.credential_generation) == ("ativa", 2)
    assert link.certificate_valid_to == today + dt.timedelta(days=364)


def test_removed_certificate_is_drift(world: World) -> None:
    activate(world)
    world.fake.company.update(
        certificado_cnpj=None, certificado_valido_de=None, certificado_valido_ate=None
    )
    sync_company_status(world.reload())
    assert world.reload().credential_state == "divergente"


def test_no_certificate_yet_changes_no_credential_state(world: World) -> None:
    world.fake.company.update(
        certificado_cnpj=None, certificado_valido_de=None, certificado_valido_ate=None
    )
    outcome = sync_company_status(world.link)
    link = world.reload()
    assert outcome.state == CallState.SUCCESS
    assert (link.credential_state, link.credential_generation) == ("ausente", 0)
    assert link.provider_flags["certificado_especifico"] is False


def test_company_of_another_cnpj_is_drift(world: World) -> None:
    activate(world)
    world.fake.company["cnpj"] = OTHER_CNPJ
    sync_company_status(world.reload())
    assert world.reload().credential_state == "divergente"


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        json.dumps({"id": COMPANY_ID + 1, "cnpj": CNPJ}).encode(),
        json.dumps(
            {"id": COMPANY_ID, "cnpj": CNPJ, "certificado_valido_ate": "amanha"}
        ).encode(),
    ],
)
def test_unreadable_or_foreign_company_reply_writes_nothing(
    world: World, body: bytes
) -> None:
    activate(world)
    before = world.reload()
    world.fake.script.append(Reply(status=200, body=body))
    outcome = sync_company_status(before)
    after = world.reload()
    assert outcome.state == CallState.FAILED
    assert (after.version, after.credential_state, after.provider_flags) == (
        before.version,
        before.credential_state,
        before.provider_flags,
    )
    assert world.issues() == set()


def test_sync_refused_while_focus_is_paused(
    world: World, settings: SettingsWrapper
) -> None:
    settings.FOCUS_PAUSED = True
    with pytest.raises(AdmissionDeniedError):
        sync_company_status(world.link)
    assert world.fake.requests == []


def test_sync_refused_for_a_tenant_without_document_capture(world: World) -> None:
    Tenant.objects.filter(pk=world.tenant.pk).update(
        features=["cycles_r1", "scan_required"]
    )
    with pytest.raises(AdmissionDeniedError):
        sync_company_status(world.link)
    assert world.fake.requests == []


# --- Admission of configuration versus capture ---------------------------------


def test_capture_read_is_refused_on_a_link_still_being_configured(
    world: World,
) -> None:
    # Configuration calls run on a pending link; capture reads never do.
    activate(world, status="configuracao_pendente")
    with pytest.raises(AdmissionDeniedError):
        reserve(world.tenant.pk, world.link.pk, _capture_request())
    sync_company_status(world.reload())
    assert [r.method for r in world.fake.requests] == ["GET"]


def test_capture_read_is_refused_on_a_link_being_disabled(world: World) -> None:
    activate(world, disablement_state="desativacao_no_provedor_pendente")
    with pytest.raises(AdmissionDeniedError):
        reserve(world.tenant.pk, world.link.pk, _capture_request())


def test_configuration_is_refused_on_a_revoked_link(world: World) -> None:
    activate(world, status="revogada")
    with pytest.raises(AdmissionDeniedError):
        sync_company_status(world.reload())
    assert world.fake.requests == []


def test_capture_links_requires_every_condition(world: World) -> None:
    activate(world)
    assert len(_capture(world)) == 1
    changes: list[dict[str, object]] = [
        {"status": "configuracao_pendente"},
        {"disablement_state": "pausada_localmente"},
        {"credential_state": "confirmacao_pendente"},
        {"streams": []},
    ]
    for change in changes:
        activate(world, **change)
        assert _capture(world) == [], change
    activate(world, credential_state="vencendo")
    assert len(_capture(world)) == 1
    with tenant_context(world.tenant.pk):
        ProviderConnection.objects.update(status="pausada")
    assert _capture(world) == []


def test_capture_links_requires_a_capture_start_month(world: World) -> None:
    # The start month is immutable once set, so this link never had one.
    activate(world, capture_start_month=None)
    assert _capture(world) == []


def _capture(world: World) -> list[ClientProviderLink]:
    with tenant_context(world.tenant.pk):
        return list(capture_links())


def _capture_request() -> CallRequest:
    digest = "a" * 64
    return CallRequest("FC-NFE", "b" * 64, digest, "c" * 64)


# --- Rate limiter -------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_rate_limiter_admits_sixty_per_minute_per_token() -> None:
    clock = Clock()
    limiter = TokenRateLimiter(clock=clock)
    # Given 60 sends one second apart (t = 1000 .. 1059).
    for second in range(60):
        clock.now = 1000.0 + second
        assert limiter.acquire("token-a") is True
    # Then the 61st inside the minute is refused; another token has its own window.
    clock.now = 1059.5
    assert limiter.acquire("token-a") is False
    assert limiter.acquire("token-b") is True
    # And the window slides: exactly 60 s after the oldest send, one slot frees.
    clock.now = 1060.0
    assert limiter.acquire("token-a") is True
    assert limiter.acquire("token-a") is False
    clock.now = 1060.5
    assert limiter.acquire("token-a") is False
    clock.now = 1061.0
    assert limiter.acquire("token-a") is True


def test_refused_acquire_does_not_consume_a_slot() -> None:
    clock = Clock()
    limiter = TokenRateLimiter(limit=1, clock=clock)
    assert limiter.acquire("k") is True
    clock.now += 30
    assert limiter.acquire("k") is False
    clock.now += 30
    assert limiter.acquire("k") is True


def test_exhausted_limiter_sends_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the Token Master's window is full.
    limiter = TokenRateLimiter(limit=0)
    monkeypatch.setattr(focus_adapter, "LIMITER", limiter)
    # When status is synced.
    outcome = sync_company_status(world.link)
    # Then Focus never sees the request and the call failed definitively.
    assert world.fake.requests == []
    assert outcome.state == CallState.FAILED
    assert world.calls()[0].provider_code == "limite_local_de_requisicoes"


def test_focus_429_on_a_configuration_call_is_a_definitive_failure(
    world: World,
) -> None:
    world.fake.script.append(Reply(status=429, body=b"{}"))
    with pytest.raises(CommandError):
        setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    assert [c.state for c in world.calls()] == [CallState.FAILED]


def test_adapter_sends_basic_auth_with_the_token_as_user(world: World) -> None:
    sync_company_status(world.link)
    headers = dict(world.fake.requests[0].headers)
    # base64("synthetic-focus:") -- the fake environment's synthetic token.
    assert headers["Authorization"] == "Basic c3ludGhldGljLWZvY3VzOg=="


def test_real_environment_without_the_deployment_secret_refuses_to_plan(
    world: World, settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.FOCUS_ENVIRONMENT = "homologacao"
    with tenant_context(world.tenant.pk):
        ProviderConnection.objects.update(environment="homologacao")
    monkeypatch.delenv("FOCUS_SECRET_FOCUS_TOKEN_MASTER", raising=False)
    with pytest.raises(CommandError, match="credencial"):
        setup(world, "--streams", "nfsen_recebidas", "--start-month", "2026-11")
    assert world.fake.requests == []


def test_adapter_refuses_a_request_that_differs_from_its_digest(world: World) -> None:
    adapter = FocusAdapter(
        ProviderTransport("focus", "fake", world.fake.origin),
        ProviderRequest(variant="read", subject=str(COMPANY_ID)),
        token=focus_adapter.FAKE_TOKEN,
        limiter_key="fake:test",
    )
    assert adapter.matches(focus_adapter.request_digest(adapter.request))
    assert not adapter.matches("0" * 64)


# --- Custody: no parameter can carry key material -----------------------------

FORBIDDEN_PARAMETER = re.compile(
    r"cert|certific|senha|password|passwd|pfx|p12|pkcs|private|chave_privada|key_file",
    re.IGNORECASE,
)


def _focus_callables() -> Iterator[tuple[str, object]]:
    for module_info in pkgutil.walk_packages(
        focus_package.__path__, focus_package.__name__ + "."
    ):
        module = importlib.import_module(module_info.name)
        for name, value in vars(module).items():
            if getattr(value, "__module__", None) != module.__name__:
                continue
            if inspect.isfunction(value):
                yield f"{module.__name__}.{name}", value
            elif inspect.isclass(value):
                for member_name, member in vars(value).items():
                    if inspect.isfunction(member):
                        yield f"{module.__name__}.{name}.{member_name}", member


def test_no_adapter_function_accepts_certificate_or_password_parameters() -> None:
    # Given every function and method of the Focus package.
    checked = 0
    for qualified, function in _focus_callables():
        assert callable(function)
        for parameter in inspect.signature(function).parameters:
            # Then none takes anything named like key material (Q1, ADR-0008).
            assert not FORBIDDEN_PARAMETER.search(parameter), (qualified, parameter)
        checked += 1
    assert checked > 10


@pytest.mark.parametrize("command", ["focus_company_setup", "focus_company_disable"])
def test_operator_commands_accept_no_certificate_or_password_option(
    command: str,
) -> None:
    module = importlib.import_module(f"apps.integrations.management.commands.{command}")
    parser = module.Command().create_parser("manage.py", command)
    options = [option for action in parser._actions for option in action.option_strings]
    assert "--client" in options
    for option in options:
        assert not FORBIDDEN_PARAMETER.search(option), option


def test_no_web_route_reaches_the_operator_commands() -> None:
    from django.urls import get_resolver  # noqa: PLC0415

    def callbacks(resolver: object) -> Iterator[str]:
        for pattern in getattr(resolver, "url_patterns", []):
            yield from callbacks(pattern)
            callback = getattr(pattern, "callback", None)
            if callback is not None:
                yield f"{callback.__module__}.{callback.__qualname__}"

    assert not [name for name in callbacks(get_resolver()) if ".focus" in name]


# --- Qualification scenario ----------------------------------------------------


def test_qualify_provider_runs_the_empresa_status_scenario_without_secrets(
    world: World,
) -> None:
    out = io.StringIO()
    call_command(
        "qualify_provider",
        "focus",
        "--scenario",
        "empresa_status",
        "--environment",
        "fake",
        stdout=out,
    )
    files = list(Path(".evidence").glob("AXRM-PQ-focus-empresa_status-*.txt"))
    assert len(files) == 1
    raw = files[0].read_text()
    evidence = json.loads(raw)
    assert evidence["http_status"] == 200
    assert evidence["billing_class"] == "incluido_no_plano"
    assert evidence["parsed_fields"]["certificate_reported"] is True
    assert evidence["parsed_fields"]["credential_state"] == "confirmacao_pendente"
    for secret in (*TOKENS, "synthetic-focus"):
        assert secret not in raw


def test_qualify_provider_refuses_focus_production() -> None:
    with pytest.raises(CommandError):
        call_command(
            "qualify_provider",
            "focus",
            "--scenario",
            "empresa_status",
            "--environment",
            "producao",
        )
