"""The `sitfis` qualification scenario (todo 13's registry; PQ-SERPRO-1 harness).

`fake` runs the ordinary SITFIS flow against an owned loopback peer answering
the documented shapes (protocol with `tempoEspera`, then the PDF). `trial` runs
the same code against the fixed trial gateway and waits the provider-mandated
time between attempts; that run is operator provider qualification, never this
repository's acceptance. `producao` is not registered.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from contextlib import contextmanager
from datetime import timedelta
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
from apps.integrations.serpro.qualification import (
    QualificationEvidence,
    Scenario,
    SyntheticTermo,
    register_scenario,
    synthetic_termo,
)
from apps.integrations.serpro.sitfis import (
    FINAL_STATES,
    advance_situation_report,
    request_situation_report,
)
from apps.obligations.models import Document, FiscalReportState

if TYPE_CHECKING:
    from collections.abc import Iterator

    from apps.integrations.transport_types import Environment, JSONValue

SYNTHETIC_PRICES: Final = {"serpro_consulta": "0.24", "serpro_emissao": "0.32"}
SYNTHETIC_PDF: Final = b"%PDF-1.7\n% synthetic qualification report\n%%EOF\n"
# The longest provider-mandated wait the operator run sits through per attempt.
MAX_QUALIFICATION_WAIT: Final = timedelta(minutes=5)


def _envelope(dados: dict[str, object], status: int = 200) -> bytes:
    return json.dumps(
        {"status": status, "mensagens": [], "dados": json.dumps(dados)}
    ).encode()


@contextmanager
def _fake_origin() -> Iterator[str]:
    """Serve the termo, the protocol (no wait) and the report on owned loopback."""
    expiry = (timezone.now() + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    replies = {
        "ENVIOXMLASSINADO81": _envelope(
            {
                "autenticar_procurador_token": "synthetic-pq-sitfis",
                "data_hora_expiracao": expiry,
            }
        ),
        "SOLICITARPROTOCOLO91": _envelope(
            {"protocoloRelatorio": "synthetic-pq-protocol", "tempoEspera": 0}
        ),
        "RELATORIOSITFIS92": _envelope(
            {"pdf": base64.b64encode(SYNTHETIC_PDF).decode()}
        ),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: str) -> None:  # noqa: A002
            """Synthetic payloads are not logged either."""

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            try:
                service = json.loads(raw)["pedidoDados"]["idServico"]
            except (ValueError, KeyError, TypeError):
                service = ""
            body = replies.get(service, b"")
            self.send_response(200 if body else 404)
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


def _admit(environment: Environment, origin: str) -> SyntheticTermo:
    """Accept the termo, link the client and declare and verify 00002."""
    world = synthetic_termo(environment)
    today = timezone.localdate()
    with tenant_context(world.tenant.pk):
        version = ProviderConnection.objects.get(pk=world.connection.pk).version
    submit_termo(
        world.actor,
        world.tenant.pk,
        TermoSubmission(
            world.connection.pk, world.document.pk, version, world.parties, origin
        ),
    )
    with tenant_context(world.tenant.pk):
        ProviderBudget.objects.update(cap_microbrl=1_000_000)
        ClientProviderLink.objects.create(
            tenant=world.tenant,
            client=world.client,
            connection=world.connection,
            provider_company_ref="pq-sitfis",
            status="ativa",
        )
        declare_authority(
            world.actor,
            world.client.pk,
            service="sitfis",
            code="00002",
            valid_from=today - timedelta(days=30),
            expected_version=0,
        )
        head = head_record(world.client.pk, "sitfis")
        verify_authority(
            world.actor,
            world.client.pk,
            service="sitfis",
            expected_version=head.version if head else 0,
        )
    return world


def _call_fields(call: ProviderCall) -> dict[str, JSONValue]:
    return {
        "operation": call.operation,
        "state": call.state,
        "http_status": call.http_status,
        "billed": call.billed,
        "request_digest": call.request_digest,
        "response_digest": call.response_digest,
    }


def _sitfis_case(environment: Environment, origin: str) -> QualificationEvidence:
    caps = dict(settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL)
    prices = dict(settings.USAGE_UNIT_PRICES_BRL)
    if environment == "fake":
        caps["serpro"] = "10.00"
        prices = {**prices, **SYNTHETIC_PRICES}
    start = monotonic()
    world = None
    try:
        with override_settings(
            PROVIDERS_PAUSED=False,
            SERPRO_PAUSED=False,
            SERPRO_ENVIRONMENT=environment,
            SERPRO_FAKE_ORIGIN=origin,
            PROVIDER_GLOBAL_MONTHLY_CAP_BRL=caps,
            USAGE_UNIT_PRICES_BRL=prices,
            # The synthetic firm requires scanning; its original stays quarantined.
            CLAMAV_ENABLED=True,
        ):
            world = _admit(environment, origin)
            client = world.client
            tenant_id = world.tenant.pk
            with tenant_context(tenant_id):
                report = request_situation_report(client, enqueue=False)
            report = advance_situation_report(tenant_id, report.pk)
            while (
                report.state == FiscalReportState.AGUARDANDO
                and report.wait_until is not None
            ):
                # Operator run: honour the provider-mandated wait, bounded.
                pause = (report.wait_until - timezone.now()).total_seconds()
                time.sleep(min(max(pause, 0), MAX_QUALIFICATION_WAIT.total_seconds()))
                report = advance_situation_report(tenant_id, report.pk)
        elapsed = monotonic() - start
        with tenant_context(tenant_id):
            calls = list(ProviderCall.objects.order_by("created_at", "pk"))
            document = (
                Document.objects.get(pk=report.document_id)
                if report.document_id
                else None
            )
    finally:
        if world is not None:
            tenant = world.tenant
            tenant.is_active = False
            tenant.save(update_fields=["is_active"])
    return QualificationEvidence(
        calls[0].request_digest if calls else "",
        calls[-1].response_digest if calls else "",
        calls[-1].http_status if calls else None,
        "emissao",
        {
            "state": report.state,
            "final": report.state in FINAL_STATES,
            "emission_attempts": report.emission_attempts,
            "protocol_digest": hashlib.sha256(report.protocol.encode()).hexdigest()
            if report.protocol
            else None,
            "document_sha256": document.sha256 if document else None,
            "calls": [_call_fields(call) for call in calls],
        },
        elapsed,
    )


def qualify_sitfis(environment: Environment) -> QualificationEvidence:
    """Use the owned fake loopback peer or the fixed trial transport, never producao."""
    if environment == "fake":
        with _fake_origin() as peer:
            return _sitfis_case(environment, peer)
    return _sitfis_case(environment, "")


register_scenario(
    Scenario("serpro", "sitfis", frozenset({"fake", "trial"}), qualify_sitfis)
)
