"""The `pagtoweb` qualification scenario (todo 13's registry; PQ-SERPRO-2 harness).

`fake` runs the ordinary paged consultation against an owned loopback peer that
answers the documented shapes (100 + 3 documents). `trial` runs the same code
against the fixed trial gateway; that run is operator provider qualification,
never this repository's acceptance. `producao` is not registered.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from time import monotonic
from typing import TYPE_CHECKING, Final

from django.conf import settings
from django.test import override_settings
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.integrations.authority import declare_authority, head_record, verify_authority
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.call_models import ProviderCall
from apps.integrations.models import ClientProviderLink, ProviderConnection
from apps.integrations.serpro.authorization import TermoSubmission, submit_termo
from apps.integrations.serpro.pagtoweb import PaymentRange
from apps.integrations.serpro.payments import consult_payments
from apps.integrations.serpro.qualification import (
    QualificationEvidence,
    Scenario,
    register_scenario,
    synthetic_termo,
)
from apps.obligations.models import Obligation

if TYPE_CHECKING:
    from collections.abc import Iterator

    from apps.integrations.transport_types import Environment

SYNTHETIC_PRICES: Final = {"serpro_consulta": "0.24", "serpro_emissao": "0.32"}


def _document(numero: str, code: str, period: str) -> dict[str, object]:
    return {
        "numeroDocumento": numero,
        "tipo": {"codigo": code, "descricao": "SINTETICO", "descricaoAbreviada": ""},
        "periodoApuracao": f"{period}T00:00:00-03:00",
        "dataArrecadacao": "2026-10-20T00:00:00-03:00",
        "dataVencimento": "2026-10-20T00:00:00-03:00",
        "receitaPrincipal": {"codigo": "1001", "descricao": None},
        "valorTotal": 76.6,
        "valorPrincipal": 76.6,
        "valorMulta": None,
        "valorJuros": None,
    }


def _pages(competence: date) -> list[bytes]:
    darfs = [_document(f"PQ{n:014d}", "04", "2026-08-31") for n in range(102)]
    das = _document("PQDAS00000000001", "09", competence.isoformat())
    return [
        json.dumps(
            {"status": 200, "mensagens": [], "dados": json.dumps(chunk)}
        ).encode()
        for chunk in (darfs[:100], [*darfs[100:], das])
    ]


@contextmanager
def _fake_origin(competence: date) -> Iterator[str]:
    """Serve the termo answer, then the two scripted pages, on owned loopback."""
    expiry = (timezone.now() + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    termo = json.dumps(
        {
            "status": 200,
            "mensagens": [],
            "dados": json.dumps(
                {
                    "autenticar_procurador_token": "synthetic-pq-pagtoweb",
                    "data_hora_expiracao": expiry,
                }
            ),
        }
    ).encode()
    pages = _pages(competence)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: str) -> None:  # noqa: A002
            """Synthetic payloads are not logged either."""

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path == "/Apoiar":
                status, body = 200, termo
            elif self.path == "/Consultar" and pages:
                status, body = 200, pages.pop(0)
            else:
                status, body = 404, b""
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    closed = threading.Event()

    def serve() -> None:
        while not closed.is_set():
            server.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        closed.set()
        server.socket.shutdown(2)
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            message = "fake_nao_encerrou"
            raise RuntimeError(message)


def _pagtoweb_case(environment: Environment, origin: str) -> QualificationEvidence:
    """Accept the termo, declare and verify 00004, then consult through the protocol."""
    world = synthetic_termo(environment)
    today = timezone.localdate()
    competence = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    caps = dict(settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL)
    prices = dict(settings.USAGE_UNIT_PRICES_BRL)
    if environment == "fake":
        caps["serpro"] = "10.00"
        prices = {**prices, **SYNTHETIC_PRICES}
    start = monotonic()
    try:
        with override_settings(
            PROVIDERS_PAUSED=False,
            SERPRO_PAUSED=False,
            SERPRO_ENVIRONMENT=environment,
            SERPRO_FAKE_ORIGIN=origin,
            PROVIDER_GLOBAL_MONTHLY_CAP_BRL=caps,
            USAGE_UNIT_PRICES_BRL=prices,
        ):
            with tenant_context(world.tenant.pk):
                version = ProviderConnection.objects.get(pk=world.connection.pk).version
            submit_termo(
                world.actor,
                world.tenant.pk,
                TermoSubmission(
                    world.connection.pk,
                    world.document.pk,
                    version,
                    world.parties,
                    origin,
                ),
            )
            with tenant_context(world.tenant.pk):
                ProviderBudget.objects.update(cap_microbrl=1_000_000)
                ClientProviderLink.objects.create(
                    tenant=world.tenant,
                    client=world.client,
                    connection=world.connection,
                    provider_company_ref="pq-pagtoweb",
                    status="ativa",
                )
                Obligation.objects.create(
                    tenant=world.tenant,
                    client=world.client,
                    obligation_type_id="DAS",
                    competence_month=competence,
                    nominal_due_date=today,
                    resolved_due_date=today,
                )
                declare_authority(
                    world.actor,
                    world.client.pk,
                    service="pagtoweb",
                    code="00004",
                    valid_from=today - timedelta(days=30),
                    expected_version=0,
                )
                head = head_record(world.client.pk, "pagtoweb")
                verify_authority(
                    world.actor,
                    world.client.pk,
                    service="pagtoweb",
                    expected_version=head.version if head else 0,
                )
            query = consult_payments(
                world.client,
                PaymentRange(competence, today),
                actor=None,
                nonce="qualificacao",
            )
        elapsed = monotonic() - start
        with tenant_context(world.tenant.pk):
            calls = list(
                ProviderCall.objects.filter(operation="SP-PAG").order_by("created_at")
            )
    finally:
        world.tenant.is_active = False
        world.tenant.save(update_fields=["is_active"])
    return QualificationEvidence(
        calls[0].request_digest if calls else "",
        calls[-1].response_digest if calls else "",
        calls[-1].http_status if calls else None,
        "consulta",
        {
            "state": query.state,
            "pages_fetched": query.pages_fetched,
            "payments_found": query.payments_found,
            "matched_das": query.matched_count,
            "page_calls": [
                {
                    "state": call.state,
                    "http_status": call.http_status,
                    "billed": call.billed,
                    "request_digest": call.request_digest,
                    "response_digest": call.response_digest,
                }
                for call in calls
            ],
        },
        elapsed,
    )


def qualify_pagtoweb(environment: Environment) -> QualificationEvidence:
    """Use the owned fake loopback peer or the fixed trial transport, never producao."""
    if environment == "fake":
        today = timezone.localdate()
        competence = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        with _fake_origin(competence) as peer:
            return _pagtoweb_case(environment, peer)
    return _pagtoweb_case(environment, "")


register_scenario(
    Scenario("serpro", "pagtoweb", frozenset({"fake", "trial"}), qualify_pagtoweb)
)
