"""`qualify_provider focus --scenario empresa_status` (todo 13's extension point).

Reads one company through the ordinary protocol and the real `sync_company_status`
path, for a fresh synthetic firm that is left inactive afterwards. `fake` serves a
synthetic company from an owned loopback peer; `homologacao` reads the Focus test
company named by `FOCUS_QUALIFICATION_COMPANY_REF` (CNPJ in
`FOCUS_QUALIFICATION_CNPJ`) with the deployment secret `focus/qualificacao`.
Production is not registered. Homologation has no fiscal validity, so this
evidence never closes PQ-FOCUS-1 or OG-R4 by itself.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from time import monotonic
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.budget_pricing import FOCUS_PLAN
from apps.integrations.call_models import ProviderCall
from apps.integrations.focus.company import sync_company_status
from apps.integrations.models import ClientProviderLink, ProviderConnection
from apps.integrations.serpro.qualification import (
    QualificationEvidence,
    Scenario,
    register_scenario,
)
from apps.tenants.models import Membership, Tenant, TenantRole

if TYPE_CHECKING:
    from collections.abc import Iterator

    from apps.integrations.transport_types import Environment, JSONValue

SYNTHETIC_CNPJ: Final = "12ABC34501DE35"
SYNTHETIC_COMPANY: Final = "900001"
QUALIFICATION_ACCOUNT: Final = "focus/qualificacao"


@contextmanager
def _fake_company_origin() -> Iterator[str]:
    """Serve one synthetic company read from an owned loopback peer."""
    today = timezone.localdate()
    first = today.replace(day=1)
    payload = json.dumps(
        {
            "id": int(SYNTHETIC_COMPANY),
            "cnpj": SYNTHETIC_CNPJ,
            "certificado_cnpj": SYNTHETIC_CNPJ,
            "certificado_valido_de": f"{first.isoformat()}T00:00:00-03:00",
            "certificado_valido_ate": f"{today.year + 1}-01-01T00:00:00-03:00",
            "habilita_nfsen_recebidas_homologacao": False,
            "habilita_manifestacao_homologacao": False,
            "habilita_filtro_mde_destinatario_empresa": False,
            "certificado_especifico": False,
        }
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: str) -> None:  # noqa: A002
            """Synthetic payloads are not logged either."""

        def do_GET(self) -> None:
            found = self.path == f"/v2/empresas/{SYNTHETIC_COMPANY}"
            body = payload if found else b"{}"
            self.send_response(200 if found else 404)
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


@dataclass(frozen=True, slots=True)
class _World:
    tenant: Tenant
    link: ClientProviderLink


def _synthetic_world(environment: Environment, company: str, cnpj: str) -> _World:
    # PLATFORM_QUERY_OK: the operator creates a fresh, explicitly synthetic firm.
    tenant = Tenant.objects.create(
        name="Qualificacao Focus sintetica",
        slug=f"pq-focus-{uuid4().hex[:12]}",
        features=["cycles_r1", "scan_required", "document_capture"],
    )
    actor = User.objects.create_user(email=f"pq-focus-{uuid4().hex}@example.test")
    # PLATFORM_QUERY_OK: the firm-side owner of this new synthetic tenant.
    Membership.objects.create(tenant=tenant, user=actor, role=TenantRole.OWNER)
    with tenant_context(tenant.pk):
        client = ClientCompany.objects.create(
            tenant=tenant, legal_name="Contribuinte sintetico", cnpj=cnpj, is_mei=False
        )
        connection = ProviderConnection.objects.create(
            tenant=tenant,
            provider="focus",
            environment=environment,
            status="ativa",
            secret_ref="" if environment == "fake" else QUALIFICATION_ACCOUNT,
        )
        link = ClientProviderLink.objects.create(
            tenant=tenant,
            client=client,
            connection=connection,
            provider_company_ref=company,
        )
        ProviderBudget.objects.create(
            tenant=tenant,
            provider="focus",
            window_start=timezone.now().date().replace(day=1),
            cap_microbrl=0,
            tariff_revision=FOCUS_PLAN,
        )
    return _World(tenant, link)


def _status_case(
    environment: Environment, origin: str, company: str, cnpj: str
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
            outcome = sync_company_status(world.link)
        elapsed = monotonic() - start
        with tenant_context(world.tenant.pk):
            call = ProviderCall.objects.get(pk=outcome.call_id)
            link = ClientProviderLink.objects.get(pk=world.link.pk)
    finally:
        world.tenant.is_active = False
        world.tenant.save(update_fields=["is_active"])
    flags: dict[str, JSONValue] = dict(link.provider_flags)
    return QualificationEvidence(
        call.request_digest,
        call.response_digest,
        call.http_status,
        call.billing_class,
        {
            "call_state": call.state,
            "certificate_reported": link.certificate_valid_to is not None,
            "certificate_cnpj_matches": link.certificate_cnpj == cnpj,
            "certificate_valid_to": link.certificate_valid_to.isoformat()
            if link.certificate_valid_to
            else None,
            "credential_state": link.credential_state,
            "flags": flags,
        },
        elapsed,
    )


def qualify_empresa_status(environment: Environment) -> QualificationEvidence:
    """Fake: owned loopback company. Homologation: the configured Focus test company."""
    if environment == "fake":
        with _fake_company_origin() as origin:
            return _status_case(environment, origin, SYNTHETIC_COMPANY, SYNTHETIC_CNPJ)
    company = os.environ.get("FOCUS_QUALIFICATION_COMPANY_REF", "")
    cnpj = os.environ.get("FOCUS_QUALIFICATION_CNPJ", "")
    if not company or not cnpj:
        message = "empresa_de_qualificacao_ausente"
        raise RuntimeError(message)
    return _status_case(environment, "", company, cnpj)


register_scenario(
    Scenario(
        "focus",
        "empresa_status",
        frozenset({"fake", "homologacao"}),
        qualify_empresa_status,
    )
)
