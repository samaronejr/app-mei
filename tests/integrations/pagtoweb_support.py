"""Documented PAGTOWEB shapes and a fully admitted synthetic firm for AXRM-015."""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.integrations.authority import declare_authority, head_record, verify_authority
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.call_models import ProviderCall
from apps.integrations.models import ClientProviderLink, ProviderConnection
from apps.integrations.serpro.authorization import TermoSubmission, submit_termo
from apps.integrations.serpro.models import PaymentQuery
from apps.integrations.serpro.pagtoweb import PaymentRange
from apps.integrations.serpro.payments import consult_payments
from apps.integrations.serpro.qualification import SyntheticTermo, synthetic_termo
from apps.obligations.models import Obligation
from tests.fakes.provider import Received, Reply
from tests.fakes.serpro import FakeSerpro

COMPETENCE = date(2026, 9, 1)
WINDOW = PaymentRange(COMPETENCE, date(2026, 10, 27))
PDF = b"%PDF-1.7\n% synthetic receipt AXRM-015\n%%EOF\n"
CONSULTA_MICROBRL = 240_000


def item(  # noqa: PLR0913 - one documented DocumentoArrecadacaoIC
    numero: str,
    *,
    tipo: str = "09",
    periodo: str = "2026-09-01T00:00:00-03:00",
    total: float = 76.6,
    principal: float | None = 76.6,
    multa: float | None = None,
    juros: float | None = None,
) -> dict[str, Any]:
    """One `DocumentoArrecadacaoIC` in the published field layout."""
    return {
        "numeroDocumento": numero,
        "tipo": {
            "codigo": tipo,
            "descricao": "DOCUMENTO DE ARRECADACAO DO SIMPLES NACIONAL",
            "descricaoAbreviada": "DOCUMENTO DE ARRECADACAO DO SIMPLES NACIONAL",
        },
        "periodoApuracao": periodo,
        "dataArrecadacao": "2026-10-20T00:00:00-03:00",
        "dataVencimento": "2026-10-20T00:00:00-03:00",
        "receitaPrincipal": {
            "codigo": "1001",
            "descricao": "SIMPLES NACIONAL - MEI",
            "extensaoReceita": None,
        },
        "referencia": None,
        "valorTotal": total,
        "valorPrincipal": principal,
        "valorMulta": multa,
        "valorJuros": juros,
        "valorSaldoTotal": 0,
        "valorSaldoPrincipal": 0,
        "valorSaldoMulta": 0,
        "valorSaldoJuros": 0,
        "desmembramentos": [],
    }


def darfs(count: int, start: int = 0) -> list[dict[str, Any]]:
    """Unrelated DARF documents: listed, never associated with a DAS obligation."""
    return [
        item(f"DARF{start + n:012d}", tipo="04", periodo="2026-08-31T00:00:00-03:00")
        for n in range(count)
    ]


def page_body(items: list[dict[str, Any]], status: int = 200) -> bytes:
    """The documented envelope: `dados` is an escaped JSON array."""
    return json.dumps(
        {"status": status, "mensagens": [], "dados": json.dumps(items)}
    ).encode()


def page(items: list[dict[str, Any]]) -> Reply:
    return Reply(body=page_body(items))


def receipt_body(dados: str) -> bytes:
    """The documented COMPARRECADACAO72 envelope around `dados`."""
    return json.dumps({"status": 200, "mensagens": [], "dados": dados}).encode()


def denied(body: bytes | None = None) -> Reply:
    """A complete 403 (billed on /Consultar and /Emitir)."""
    default = json.dumps(
        {
            "status": 403,
            "mensagens": [{"codigo": "Erro-Procuracao-00004", "texto": "synthetic"}],
            "dados": "",
        }
    ).encode()
    return Reply(status=403, body=default if body is None else body)


def termo_reply() -> Reply:
    """SERPRO accepting the synthetic signed termo for 30 days."""
    until = timezone.localdate() + timedelta(days=30)
    return Reply(
        body=json.dumps(
            {
                "status": 200,
                "mensagens": [],
                "dados": json.dumps(
                    {
                        "autenticar_procurador_token": "synthetic-procurador-015",
                        "data_hora_expiracao": f"{until.isoformat()}T00:00:00",
                    }
                ),
            }
        ).encode()
    )


@dataclass
class World:
    """An admitted synthetic firm: termo accepted, link active, 00004 verified."""

    termo: SyntheticTermo
    link: ClientProviderLink
    obligation: Obligation
    gateway: FakeSerpro

    def script(self, *replies: Reply) -> None:
        self.gateway.replies.extend(replies)

    def consult(self, nonce: str = "consulta-1") -> PaymentQuery:
        return consult_payments(self.termo.client, WINDOW, actor=None, nonce=nonce)

    def sent(self, path: str = "/Consultar") -> list[Received]:
        return [request for request in self.gateway.requests if request.path == path]

    def offsets(self) -> list[int]:
        return [
            json.loads(json.loads(request.body)["pedidoDados"]["dados"])[
                "primeiroDaPagina"
            ]
            for request in self.sent()
        ]

    def calls(self, operation: str = "SP-PAG") -> list[ProviderCall]:
        with tenant_context(self.termo.tenant.pk):
            return list(
                ProviderCall.objects.filter(operation=operation).order_by("created_at")
            )

    def budget(self) -> ProviderBudget:
        with tenant_context(self.termo.tenant.pk):
            return ProviderBudget.objects.get()


def declare_and_verify(termo: SyntheticTermo) -> None:
    """Record 00004 for PAGTOWEB the way staff do: declaration, then verification."""
    client = termo.client
    with tenant_context(termo.tenant.pk):
        head = head_record(client.pk, "pagtoweb")
        declare_authority(
            termo.actor,
            client.pk,
            service="pagtoweb",
            code="00004",
            valid_from=date(2026, 1, 1),
            expected_version=head.version if head else 0,
        )
        head = head_record(client.pk, "pagtoweb")
        assert head is not None
        verify_authority(
            termo.actor, client.pk, service="pagtoweb", expected_version=head.version
        )


@pytest.fixture
def world(settings: SettingsWrapper, tmp_path: Path) -> Iterator[World]:
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
                provider_company_ref="synthetic-pagtoweb",
                status="ativa",
            )
            obligation = Obligation.objects.create(
                tenant=termo.tenant,
                client=termo.client,
                obligation_type_id="DAS",
                competence_month=COMPETENCE,
                nominal_due_date=date(2026, 10, 20),
                resolved_due_date=date(2026, 10, 20),
            )
        declare_and_verify(termo)
        gateway.requests.clear()
        yield World(termo, link, obligation, gateway)
