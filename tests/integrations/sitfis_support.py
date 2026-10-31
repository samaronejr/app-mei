"""Documented SITFIS shapes, an injected clock and an admitted synthetic firm.

AXRM-016. Missing implementation is an assertion, never an import error or a
skip, so the red run fails on what the plan asks for.
"""

import base64
import importlib
import importlib.util
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import Engagement, TaxProfile
from apps.core.tenancy import tenant_context
from apps.cycles.models import PlaybookVersion, ServiceCycle
from apps.cycles.packs import install_system_packs
from apps.cycles.scanning import FakeScanner
from apps.cycles.services.cycle import open_cycle
from apps.cycles.tasks import scan_quarantine
from apps.integrations.authority import declare_authority, head_record, verify_authority
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.call_models import ProviderCall
from apps.integrations.models import ClientProviderLink, ProviderConnection
from apps.integrations.serpro.authorization import TermoSubmission, submit_termo
from apps.integrations.serpro.qualification import SyntheticTermo, synthetic_termo
from apps.obligations.models import FiscalSituationReport
from apps.tenants.models import Membership, TenantRole
from tests.fakes.provider import Received, Reply
from tests.fakes.serpro import FakeSerpro

PERIOD = date(2026, 10, 1)
START = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
PDF = b"%PDF-1.7\n% synthetic SITFIS report AXRM-016\n%%EOF\n"
EMISSAO_MICROBRL = 320_000
PROTOCOL = "+S7N6c04XNZUVzmxWT7SzpkZA4xeDQC9Z2T2GBJ5usn8LyouyWX/synthetic=="


def module(name: str) -> ModuleType:
    """Import an AXRM-016 module, failing on an assertion when it is missing."""
    assert importlib.util.find_spec(name) is not None, f"AXRM-016 must supply {name}"
    return importlib.import_module(name)


def sitfis() -> ModuleType:
    return module("apps.integrations.serpro.sitfis")


def envelope(status: int, dados: str, codes: tuple[str, ...] = ()) -> bytes:
    """The documented Integra Contador reply around an escaped `dados` string."""
    return json.dumps(
        {
            "status": status,
            "mensagens": [{"codigo": code, "texto": "sintetico"} for code in codes],
            "dados": dados,
        }
    ).encode()


def protocol_reply(wait_ms: int | None = 30_000, protocol: str = PROTOCOL) -> Reply:
    """SOLICITARPROTOCOLO91: protocol plus the estimated wait in milliseconds."""
    dados: dict[str, Any] = {"protocoloRelatorio": protocol}
    if wait_ms is not None:
        dados["tempoEspera"] = wait_ms
    return Reply(body=envelope(200, json.dumps(dados), ("[Aviso-Sitfis-AV01]",)))


def report_reply(pdf: bytes = PDF) -> Reply:
    """RELATORIOSITFIS92 200: the report PDF as base64 inside `dados`."""
    encoded = base64.b64encode(pdf).decode()
    return Reply(body=envelope(200, json.dumps({"pdf": encoded})))


def processing_202(wait_ms: int = 4_000) -> Reply:
    """Asked before the wait ended: billed, with a new `tempoEspera` in `dados`."""
    return Reply(
        status=202,
        body=envelope(
            202, json.dumps({"tempoEspera": wait_ms}), ("[Sucesso-Sitfis-SC02]",)
        ),
    )


def processing_204(wait_ms: int = 5_000) -> Reply:
    """Asked after the wait, still processing: no body, the estimate in `ETag`."""
    return Reply(status=204, body=b"", headers=(("ETag", f'"tempoEspera:{wait_ms}"'),))


def denied_403() -> Reply:
    return Reply(
        status=403,
        body=envelope(403, "", ("AcessoNegado-ICGERENCIADOR-016",)),
    )


def termo_reply() -> Reply:
    """SERPRO accepting the synthetic signed termo for 30 days."""
    until = timezone.localdate() + timedelta(days=30)
    return Reply(
        body=envelope(
            200,
            json.dumps(
                {
                    "autenticar_procurador_token": "synthetic-procurador-016",
                    "data_hora_expiracao": f"{until.isoformat()}T00:00:00",
                }
            ),
        )
    )


@dataclass
class Clock:
    """An injected wall clock; tests advance it instead of sleeping."""

    current: datetime

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> None:
        self.current += timedelta(**delta)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    current = Clock(START)
    monkeypatch.setattr(timezone, "now", current.now)
    return current


@dataclass
class World:
    """An admitted firm: termo accepted, link active, 00002 verified, cycle open."""

    termo: SyntheticTermo
    link: ClientProviderLink
    cycle: ServiceCycle
    responsible: User
    gateway: FakeSerpro
    clock: Clock

    @property
    def tenant_id(self) -> UUID:
        return self.termo.tenant.pk

    def script(self, *replies: Reply) -> None:
        self.gateway.replies.extend(replies)

    def sent(self, path: str) -> list[Received]:
        return [request for request in self.gateway.requests if request.path == path]

    def request(self) -> FiscalSituationReport:
        with tenant_context(self.tenant_id):
            return sitfis().request_situation_report(self.termo.client)

    def advance(self, report: FiscalSituationReport) -> FiscalSituationReport:
        return sitfis().advance_situation_report(self.tenant_id, report.pk)

    def reload(self, report: FiscalSituationReport) -> FiscalSituationReport:
        with tenant_context(self.tenant_id):
            return FiscalSituationReport.objects.get(pk=report.pk)

    def calls(self, operation: str) -> list[ProviderCall]:
        with tenant_context(self.tenant_id):
            return list(
                ProviderCall.objects.filter(operation=operation).order_by("created_at")
            )

    def budget(self) -> ProviderBudget:
        with tenant_context(self.tenant_id):
            return ProviderBudget.objects.get()

    def scan(self) -> None:
        """Run the platform scan sweep with the in-process fake scanner."""
        scanner = FakeScanner()
        with patch("apps.cycles.tasks.scanner_from_settings", return_value=scanner):
            scan_quarantine.apply().get()


def _declare_sitfis(termo: SyntheticTermo) -> None:
    client = termo.client
    with tenant_context(termo.tenant.pk):
        head = head_record(client.pk, "sitfis")
        declare_authority(
            termo.actor,
            client.pk,
            service="sitfis",
            code="00002",
            valid_from=date(2026, 1, 1),
            expected_version=head.version if head else 0,
        )
        head = head_record(client.pk, "sitfis")
        assert head is not None
        verify_authority(
            termo.actor, client.pk, service="sitfis", expected_version=head.version
        )


def _open_cycle(termo: SyntheticTermo, responsible: User) -> ServiceCycle:
    tenant, client = termo.tenant, termo.client
    with tenant_context(tenant.pk):
        install_system_packs(tenant)
        client.status = "active"
        client.save(update_fields=["status"])
        engagement = Engagement.objects.create(
            tenant=tenant,
            client=client,
            service_kind="mei_mensal",
            starts_on=PERIOD,
            responsible=responsible,
            provenance="staff_entry",
        )
        TaxProfile.objects.create(
            tenant=tenant,
            client=client,
            regime="mei",
            valid_from=PERIOD,
            provenance="staff_entry",
        )
        definition = PlaybookVersion.objects.get(
            playbook__key="mei-mensal", state="published"
        )
        ref = open_cycle(
            termo.actor,
            engagement.pk,
            PERIOD,
            definition.pk,
            idempotency_key="axrm016",
        )
        return ServiceCycle.objects.get(pk=ref.id)


@pytest.fixture
def world(settings: SettingsWrapper, tmp_path: Path, clock: Clock) -> Iterator[World]:
    settings.MEDIA_ROOT = tmp_path
    settings.PROVIDERS_PAUSED = False
    settings.SERPRO_PAUSED = False
    settings.SERPRO_ENVIRONMENT = "fake"
    settings.PROVIDER_REAL_DATA_APPROVED = False
    settings.CLAMAV_ENABLED = True
    settings.USAGE_UNIT_PRICES_BRL = {
        "serpro_consulta": "0.24",
        "serpro_emissao": "0.32",
    }
    settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL = {"serpro": "10.00"}
    termo = synthetic_termo("fake")
    responsible = User.objects.create_user(email="responsavel@axrm016.example.test")
    # PLATFORM_QUERY_OK: the engagement responsible joins the synthetic firm.
    Membership.objects.create(
        tenant=termo.tenant, user=responsible, role=TenantRole.STAFF_ACCOUNTANT
    )
    with FakeSerpro((termo_reply(),)) as gateway:
        settings.SERPRO_FAKE_ORIGIN = gateway.origin
        with tenant_context(termo.tenant.pk):
            version = ProviderConnection.objects.get(pk=termo.connection.pk).version
        accepted = submit_termo(
            termo.actor,
            termo.tenant.pk,
            TermoSubmission(
                termo.connection.pk,
                termo.document.pk,
                version,
                termo.parties,
                gateway.origin,
            ),
        )
        assert accepted.state == "sucesso"
        with tenant_context(termo.tenant.pk):
            ProviderBudget.objects.update(cap_microbrl=2_000_000)
            link = ClientProviderLink.objects.create(
                tenant=termo.tenant,
                client=termo.client,
                connection_id=termo.connection.pk,
                provider_company_ref="synthetic-sitfis",
                status="ativa",
            )
        _declare_sitfis(termo)
        cycle = _open_cycle(termo, responsible)
        gateway.requests.clear()
        yield World(termo, link, cycle, responsible, gateway, clock)
