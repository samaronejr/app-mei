"""Bounded synthetic qualification scenarios, never an authorized-live path."""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from time import monotonic
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake, IntakeState, ScanVerdict
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.call_models import FirmProviderCall
from apps.integrations.models import ProviderConnection
from apps.integrations.serpro.adapter import Parties
from apps.integrations.serpro.authorization import TermoSubmission, submit_termo
from apps.integrations.serpro.termo import (
    SIGNATURE_NS,
    LegalNotices,
    TermoDraft,
    unsigned_termo,
)
from apps.integrations.serpro.termo_state import termo_head
from apps.obligations.models import AcquisitionOrigin, Document
from apps.tenants.models import Membership, Tenant, TenantRole

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from apps.integrations.transport_types import Environment, JSONValue


@dataclass(frozen=True, slots=True)
class QualificationEvidence:
    """Persist digests, non-secret fields and observed transport metadata."""

    request_digest: str
    response_digest: str
    http_status: int | None
    billing_class: str
    parsed_fields: dict[str, JSONValue]
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class Scenario:
    """The extension point for the later PAGTOWEB/SITFIS/PGMEI and Focus todos."""

    provider: str
    name: str
    environments: frozenset[Environment]
    run: Callable[[Environment], QualificationEvidence]


SCENARIOS: Final[dict[tuple[str, str], Scenario]] = {}


def register_scenario(scenario: Scenario) -> None:
    """Refuse duplicate registration; no unknown scenario can become a generic call."""
    key = (scenario.provider, scenario.name)
    if key in SCENARIOS:
        message = "cenario_duplicado"
        raise ValueError(message)
    SCENARIOS[key] = scenario


@contextmanager
def _fake_termo_origin() -> Iterator[str]:
    """Provide an owned loopback peer for the bare fake CLI."""
    expiry = (timezone.now() + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    payload = json.dumps(
        {
            "status": 200,
            "mensagens": [],
            "dados": json.dumps(
                {
                    "autenticar_procurador_token": str(uuid4()),
                    "data_hora_expiracao": expiry,
                }
            ),
        }
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: str) -> None:  # noqa: A002
            """Synthetic payloads are not logged either."""

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200 if self.path == "/Apoiar" else 404)
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
            message = "fake_nao_encerrou"
            raise RuntimeError(message)


@dataclass(frozen=True, slots=True)
class SyntheticTermo:
    """A fresh synthetic firm, connection and scanned signed termo; never real data."""

    tenant: Tenant
    actor: User
    client: ClientCompany
    connection: ProviderConnection
    document: Document
    parties: Parties


def synthetic_termo(environment: Environment) -> SyntheticTermo:
    """Create newly named synthetic identities for one qualification run."""
    tenant = Tenant.objects.create(
        name="Qualificacao SERPRO sintetica",
        slug=f"pq-serpro-{uuid4().hex[:12]}",
        features=["cycles_r1", "scan_required", "federal_checks"],
    )
    actor = User.objects.create_user(email=f"pq-serpro-{uuid4().hex}@example.test")
    Membership.objects.create(tenant=tenant, user=actor, role=TenantRole.OWNER)
    parties = Parties("12345678000195", "11222333000181", "11222333000181")
    today = timezone.localdate()
    draft = TermoDraft(
        "Contratante sintetico",
        "Escritorio sintetico",
        today,
        today + timedelta(days=365),
        LegalNotices(
            "Autorizacao sintetica sem validade juridica.",
            "Somente dados ficticios de qualificacao.",
            "Qualificar o formato sem poderes reais sobre contribuintes.",
        ),
    )
    unsigned = unsigned_termo(parties, draft)
    # Deliberately synthetic signature data, never a generated or received key.
    # A trial response is mock qualification; it cannot close OG-R5.
    signature = (
        f'<Signature xmlns="{SIGNATURE_NS}"><SignedInfo/>'
        "<SignatureValue>AQ==</SignatureValue><KeyInfo><X509Data>"
        "<X509Certificate>AQ==</X509Certificate></X509Data></KeyInfo></Signature>"
    ).encode()
    raw = unsigned.replace(
        b"</termoDeAutorizacao>", signature + b"</termoDeAutorizacao>"
    )
    with tenant_context(tenant.pk):
        client = ClientCompany.objects.create(
            tenant=tenant,
            legal_name="Contribuinte sintetico",
            cnpj="12ABC34501DE35",
            is_mei=True,
        )
        connection = ProviderConnection.objects.create(
            tenant=tenant,
            provider="serpro",
            environment=environment,
            status="ativa",
        )
        ProviderBudget.objects.create(
            tenant=tenant,
            provider="serpro",
            window_start=today.replace(day=1),
            cap_microbrl=0,
            tariff_revision="serpro-free-v1",
        )
        key = default_storage.save(
            f"qualification/{tenant.pk}/{uuid4()}.xml", ContentFile(raw)
        )
        document = Document.objects.create(
            tenant=tenant,
            client=client,
            uploaded_by=actor,
            storage_key=key,
            sha256=hashlib.sha256(raw).hexdigest(),
            byte_size=len(raw),
            original_filename="termo-sintetico.xml",
            content_type="application/xml",
            client_upload=False,
            portal_visible=False,
            acquisition_origin=AcquisitionOrigin.UPLOAD_STAFF,
            retention_class="operational",
        )
        DocumentIntake.objects.create(
            tenant=tenant,
            client=client,
            document=document,
            state=IntakeState.VERIFIED,
            verdict=ScanVerdict.CLEAN,
            scan_engine="synthetic-qualification",
            scanned_at=timezone.now(),
        )
    return SyntheticTermo(tenant, actor, client, connection, document, parties)


def _termo_case(environment: Environment, origin: str) -> QualificationEvidence:
    """Run the ordinary firm protocol, then leave the synthetic firm inactive."""
    world = synthetic_termo(environment)
    caps = dict(settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL)
    # SP-TERMO is free: a zero platform ceiling admits it and refuses any price.
    caps.setdefault("serpro", "0")
    start = monotonic()
    try:
        with override_settings(
            PROVIDERS_PAUSED=False,
            SERPRO_PAUSED=False,
            SERPRO_ENVIRONMENT=environment,
            PROVIDER_GLOBAL_MONTHLY_CAP_BRL=caps,
        ):
            result = submit_termo(
                world.actor,
                world.tenant.pk,
                TermoSubmission(
                    world.connection.pk,
                    world.document.pk,
                    world.connection.version,
                    world.parties,
                    origin,
                ),
            )
        elapsed = monotonic() - start
        with tenant_context(world.tenant.pk):
            call = FirmProviderCall.objects.get(connection=world.connection)
            head = termo_head(world.connection.pk)
    finally:
        world.tenant.is_active = False
        world.tenant.save(update_fields=["is_active"])
    return QualificationEvidence(
        call.request_digest,
        call.response_digest,
        result.http_status,
        "nao_faturada",
        {
            "accepted": result.state == "sucesso",
            "authorization_status": head.status if head else "enviado",
            "valid_until": head.valid_until.isoformat()
            if head and head.valid_until
            else None,
        },
        elapsed,
    )


def qualify_termo(environment: Environment) -> QualificationEvidence:
    """Use fake loopback or the fixed trial transport; production is not registered."""
    if environment == "fake":
        origin = getattr(settings, "SERPRO_FAKE_ORIGIN", "")
        if origin:
            return _termo_case(environment, origin)
        with _fake_termo_origin() as peer:
            return _termo_case(environment, peer)
    return _termo_case(environment, "")


register_scenario(
    Scenario(
        "serpro",
        "termo",
        frozenset({"fake", "trial"}),
        qualify_termo,
    )
)
