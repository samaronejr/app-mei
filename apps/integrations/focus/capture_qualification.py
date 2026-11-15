"""Bounded received-document qualification, with no provider configuration writes."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager, nullcontext
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from time import monotonic
from typing import TYPE_CHECKING
from unittest.mock import patch
from urllib.parse import urlsplit

from django.test import override_settings
from django.utils import timezone

from apps.clients.models import ClientCompany, ClientStatus
from apps.core.tenancy import tenant_context
from apps.cycles.scanning import FakeScanner
from apps.integrations.call_models import ProviderCall
from apps.integrations.focus.capture import ensure_stream, synchronize
from apps.integrations.focus.company import sync_company_status
from apps.integrations.focus.qualification import (
    SYNTHETIC_CNPJ,
    SYNTHETIC_COMPANY,
    _synthetic_world,
)
from apps.integrations.models import AuthorityRecord, ClientProviderLink
from apps.integrations.serpro.qualification import (
    QualificationEvidence,
    Scenario,
    register_scenario,
)
from apps.tenants.models import Membership, TenantRole

if TYPE_CHECKING:
    from collections.abc import Iterator

    from apps.integrations.transport_types import Environment


@contextmanager
def _fake_origin(collection: str, cnpj: str) -> Iterator[str]:
    now = timezone.now().isoformat()
    key = "33" + "0" * 42
    nfe = collection == "nfe_recebidas"
    rows = [
        {
            "chave_nfe" if nfe else "chave_nfse": key,
            "versao": 1,
            "situacao": "autorizada" if nfe else "autorizado",
            "data_emissao": now,
            "nfe_completa": False,
            "documento_emitente": "11222333000181",
            "nome_emitente": "Emitente sintetico",
            "valor_total": "100.00",
        }
    ]
    xml = (
        '<NFSe xmlns="http://www.sped.fazenda.gov.br/nfse" versao="1.01">'
        f'<infNFSe Id="NFS{key}"><cLocIncid>3303302</cLocIncid>'
        "<emit><CNPJ>11222333000181</CNPJ><xNome>Emitente sintetico</xNome></emit>"
        '<DPS versao="1.00"><infDPS Id="DPSsynthetic">'
        f"<dhEmi>{now}</dhEmi><dCompet>{timezone.localdate().replace(day=1)}</dCompet>"
        f"<prest><CNPJ>11222333000181</CNPJ></prest><toma><CNPJ>{cnpj}</CNPJ></toma>"
        "<valores><vServPrest><vServ>100.00</vServ></vServPrest></valores>"
        "</infDPS></DPS></infNFSe></NFSe>"
    ).encode()
    path = "/v2/nfes_recebidas" if nfe else "/v2/nfsens_recebidas"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: str) -> None:  # noqa: A002
            """Never log request headers, URLs or response bodies."""

        def do_GET(self) -> None:
            target = urlsplit(self.path).path
            payload = json.dumps(rows).encode() if target == path else xml
            self.send_response(200 if target in (path, f"{path}/{key}.xml") else 404)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

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
            code = "fake_nao_encerrou"
            raise RuntimeError(code)


def _case(
    environment: Environment, collection: str, origin: str, company: str, cnpj: str
) -> QualificationEvidence:
    world = _synthetic_world(environment, company, cnpj)
    start = monotonic()
    try:
        with override_settings(
            PROVIDERS_PAUSED=False,
            FOCUS_PAUSED=False,
            FOCUS_ENVIRONMENT=environment,
            FOCUS_FAKE_ORIGIN=origin,
        ):
            if environment != "fake":
                sync_company_status(world.link)
            owner = (
                # PLATFORM_QUERY_OK: one fresh synthetic firm's qualification owner.
                Membership.objects.select_related("user")
                .get(
                    tenant_id=world.tenant.pk,
                    role=TenantRole.OWNER,
                    client__isnull=True,
                )
                .user
            )
            with tenant_context(world.tenant.pk):
                link = ClientProviderLink.objects.get(pk=world.link.pk)
                # The shared company-status scenario creates an onboarding client.
                # This fresh synthetic client must be active for capture admission.
                ClientCompany.objects.filter(pk=link.client_id).update(
                    status=ClientStatus.ACTIVE
                )
                link.status = "ativa"
                link.streams = [collection]
                link.capture_start_month = timezone.localdate().replace(day=1)
                if environment == "fake":
                    link.credential_state = "ativa"
                    link.certificate_cnpj = cnpj
                    link.certificate_valid_from = timezone.localdate()
                    link.certificate_valid_to = timezone.localdate() + timedelta(
                        days=365
                    )
                link.save()
                AuthorityRecord.objects.create(
                    tenant_id=world.tenant.pk,
                    client_id=link.client_id,
                    kind="autorizacao_captura",
                    service="captura_nfe"
                    if collection == "nfe_recebidas"
                    else "captura_nfse",
                    code="autorizacao_cliente",
                    status="ativa",
                    valid_from=timezone.localdate(),
                    declared_by=owner,
                    verified_by=owner,
                )
            fake_scan = (
                patch(
                    "apps.obligations.fiscal_ingest.scanner_from_settings",
                    return_value=FakeScanner(),
                )
                if environment == "fake"
                else nullcontext()
            )
            scan_setting = (
                override_settings(CLAMAV_ENABLED=True)
                if environment == "fake"
                else nullcontext()
            )
            with fake_scan, scan_setting:
                stream = ensure_stream(link, collection)
                run = synchronize(world.tenant.pk, stream.pk)
            if run is None or run.state not in ("concluida", "parcial"):
                code = "captura_nao_concluida"
                raise RuntimeError(code)
            with tenant_context(world.tenant.pk):
                calls = list(ProviderCall.objects.order_by("created_at", "pk"))
                stream.refresh_from_db()
            return QualificationEvidence(
                hashlib.sha256(
                    "".join(call.request_digest for call in calls).encode()
                ).hexdigest(),
                hashlib.sha256(
                    "".join(call.response_digest for call in calls).encode()
                ).hexdigest(),
                calls[-1].http_status,
                "incluido_no_plano",
                {
                    "collection": collection,
                    "environment": environment,
                    "documents": run.documents_accounted,
                    "cursor": stream.committed_cursor,
                    "complete_scan": stream.last_complete_scan_at is not None,
                },
                monotonic() - start,
            )
    finally:
        world.tenant.is_active = False
        world.tenant.save(update_fields=["is_active"])


def _qualify(environment: Environment, collection: str) -> QualificationEvidence:
    if environment == "fake":
        with _fake_origin(collection, SYNTHETIC_CNPJ) as origin:
            return _case(
                environment, collection, origin, SYNTHETIC_COMPANY, SYNTHETIC_CNPJ
            )
    company = os.environ.get("FOCUS_QUALIFICATION_COMPANY_REF", "")
    cnpj = os.environ.get("FOCUS_QUALIFICATION_CNPJ", "")
    if not company or not cnpj:
        code = "empresa_de_qualificacao_ausente"
        raise RuntimeError(code)
    return _case(environment, collection, "", company, cnpj)


def qualify_nfe(environment: Environment) -> QualificationEvidence:
    """Read the received NF-e stream; never register any manifestation."""
    return _qualify(environment, "nfe_recebidas")


def qualify_nfse(environment: Environment) -> QualificationEvidence:
    """Read the received national NFS-e stream and acquire its XML."""
    return _qualify(environment, "nfsen_recebidas")


for name, runner in (("nfe_recebidas", qualify_nfe), ("nfsen_recebidas", qualify_nfse)):
    register_scenario(
        Scenario("focus", name, frozenset({"fake", "homologacao"}), runner)
    )
