"""A synthetic MEI firm whose SERPRO termo is in force, for the PGMEI guide (AXRM-017).

Everything here is real: the termo is accepted through the ordinary firm protocol
against a loopback fake, the cycle is opened from the installed `mei-mensal` pack and
the guide is requested through the public service. No provider is ever reached.
"""

import base64
import importlib
import importlib.util
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest
from django.test import Client
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientAssignment, ClientContact, Engagement, TaxProfile
from apps.core.tenancy import tenant_context
from apps.cycles.effects.executors import execute
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.evidence import (
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    IntakeState,
    ReviewDecision,
)
from apps.cycles.extraction.service import extract_intake
from apps.cycles.models import ActionProposal, PlaybookVersion, ServiceCycle
from apps.cycles.packs import install_system_packs
from apps.cycles.scanning import FakeScanner
from apps.cycles.services.approvals import approve, propose
from apps.cycles.services.cycle import open_cycle
from apps.cycles.services.review import record_review
from apps.cycles.tasks import scan_quarantine
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.models import ClientProviderLink
from apps.integrations.serpro.authorization import TermoSubmission, submit_termo
from apps.integrations.serpro.qualification import SyntheticTermo, synthetic_termo
from apps.obligations.models import Obligation
from apps.tenants.models import Membership, TenantRole
from tests.browser.harness import mfa_session_cookie
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.ui.factories import add_member


class LazyModule:
    """Fail an acceptance test on an assertion while the module does not exist yet."""

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "_name", name)

    def _module(self) -> object:
        name = object.__getattribute__(self, "_name")
        assert importlib.util.find_spec(name) is not None, (
            f"AXRM-017 must supply {name}"
        )
        return importlib.import_module(name)

    def __getattr__(self, attribute: str) -> object:
        return getattr(self._module(), attribute)

    def __setattr__(self, attribute: str, value: object) -> None:
        setattr(self._module(), attribute, value)


FIRM_ONLY = "interno"
PERIOD = date(2026, 10, 1)
COMPETENCE = "202610"
GUIDE_PDF = b"%PDF-1.4\n% synthetic DAS-MEI guide AXRM-017\n%%EOF\n"
PRICE_MICROBRL = 320_000


def termo_reply() -> Reply:
    until = timezone.localdate() + timedelta(days=30)
    return Reply(
        body=json.dumps(
            {
                "status": 200,
                "mensagens": [],
                "dados": json.dumps(
                    {
                        "autenticar_procurador_token": "synthetic-procurador-017",
                        "data_hora_expiracao": f"{until.isoformat()}T00:00:00",
                    }
                ),
            }
        ).encode()
    )


def das_body(
    pdf: bytes = GUIDE_PDF,
    *,
    cnpj: str = "12ABC34501DE35",
    period: str = COMPETENCE,
    status: int = 200,
) -> bytes:
    """The documented GERARDASPDF21 reply: `dados` escapes a list of one `Das`."""
    das = {
        "pdf": base64.b64encode(pdf).decode(),
        "cnpjCompleto": cnpj,
        "detalhamento": {
            "periodoApuracao": period,
            "numeroDocumento": "07202630412345678",
            "dataVencimento": "20261120",
            "dataLimiteAcolhimento": "20261130",
            "valores": {"principal": 76.6, "multa": 0, "juros": 0, "total": 76.6},
        },
    }
    return json.dumps(
        {"status": status, "mensagens": [], "dados": json.dumps([das])}
    ).encode()


def das_reply(**changes: object) -> Reply:
    return Reply(body=das_body(**changes))  # type: ignore[arg-type]


@dataclass
class GuideWorld:
    termo: SyntheticTermo
    staff: User
    ops: User
    outsider: User
    link: ClientProviderLink
    engagement: Engagement
    cycle: ServiceCycle
    obligation: Obligation
    media: Path

    @property
    def tenant_id(self) -> UUID:
        return self.termo.tenant.pk

    @property
    def owner(self) -> User:
        return self.termo.actor


def _member(world: SyntheticTermo, email: str, role: str) -> User:
    user = User.objects.create_user(email=email)
    # PLATFORM_QUERY_OK: test setup creates the synthetic firm's own roster.
    Membership.objects.create(tenant=world.tenant, user=user, role=role, client=None)
    return user


def build_world(
    settings: SettingsWrapper,
    tmp_path: Path,
    *,
    environment: str = "fake",
    route: Callable[[str], None] | None = None,
) -> GuideWorld:
    media = tmp_path / "media"
    settings.MEDIA_ROOT = media
    settings.CLAMAV_ENABLED = True
    settings.PROVIDERS_PAUSED = False
    settings.SERPRO_PAUSED = False
    settings.SERPRO_ENVIRONMENT = environment
    settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL = {"serpro": "10.00"}
    settings.USAGE_UNIT_PRICES_BRL = {
        "serpro_consulta": "0.24",
        "serpro_emissao": "0.32",
    }
    world = synthetic_termo(environment)  # type: ignore[arg-type]
    world.tenant.features = [*world.tenant.features, "bulk_r1"]
    world.tenant.save(update_fields=["features"])
    with FakeSerpro((termo_reply(),)) as peer:
        if environment != "fake":
            assert route is not None
            route(peer.origin)
        accepted = submit_termo(
            world.actor,
            world.tenant.pk,
            TermoSubmission(
                world.connection.pk,
                world.document.pk,
                world.connection.version,
                world.parties,
                peer.origin if environment == "fake" else "",
            ),
        )
    assert accepted.state == "sucesso"
    staff = _member(world, "staff@pgmei.test", TenantRole.STAFF_ACCOUNTANT)
    ops = _member(world, "ops@pgmei.test", TenantRole.OPERATIONS_ADMIN)
    outsider = _member(world, "outsider@pgmei.test", TenantRole.STAFF_ACCOUNTANT)
    with tenant_context(world.tenant.pk):
        ProviderBudget.objects.update(cap_microbrl=2_000_000)
        install_system_packs(world.tenant)
        client = world.client
        client.status = "active"
        client.save(update_fields=["status"])
        ClientAssignment.objects.create(tenant=world.tenant, client=client, user=staff)
        engagement = Engagement.objects.create(
            tenant=world.tenant,
            client=client,
            service_kind="mei_mensal",
            starts_on=PERIOD,
            responsible=world.actor,
            provenance="staff_entry",
        )
        TaxProfile.objects.create(
            tenant=world.tenant,
            client=client,
            regime="mei",
            valid_from=PERIOD,
            provenance="staff_entry",
        )
        link = ClientProviderLink.objects.create(
            tenant=world.tenant,
            client=client,
            connection=world.connection,
            provider_company_ref="synthetic-pgmei",
            status="ativa",
        )
        obligation = Obligation.objects.create(
            tenant=world.tenant,
            client=client,
            obligation_type_id="DAS",
            competence_month=PERIOD,
            nominal_due_date=date(2026, 11, 20),
            resolved_due_date=date(2026, 11, 20),
        )
        definition = PlaybookVersion.objects.get(
            playbook__key="mei-mensal", state="published"
        )
        ref = open_cycle(
            world.actor,
            engagement.pk,
            PERIOD,
            definition.pk,
            idempotency_key="pgmei-world",
        )
        cycle = ServiceCycle.objects.get(pk=ref.id)
    return GuideWorld(
        world, staff, ops, outsider, link, engagement, cycle, obligation, media
    )


@pytest.fixture
def guide_world(settings: SettingsWrapper, tmp_path: Path) -> GuideWorld:
    return build_world(settings, tmp_path)


@pytest.fixture
def production_world(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[GuideWorld, Callable[[str], None]]:
    """Production mode against loopback fakes; broker tokens are per-call stubs."""
    from apps.integrations import transport  # noqa: PLC0415
    from apps.integrations.serpro import authorization  # noqa: PLC0415
    from apps.integrations.serpro.adapter import SerproAdapter  # noqa: PLC0415
    from apps.integrations.token_broker import BrokerTokens  # noqa: PLC0415

    settings.PROVIDER_REAL_DATA_APPROVED = True
    monkeypatch.setattr(
        SerproAdapter,
        "_production_tokens",
        lambda self, dispatch, connection_id: BrokerTokens(
            "synthetic-access-017", "synthetic-jwt-017", 2008, "synthetic-proc-017"
        ),
    )
    monkeypatch.setattr(authorization, "store_procurador", lambda firm, token: None)

    def route(origin: str) -> None:
        monkeypatch.setitem(transport.ORIGINS, ("serpro", "producao"), origin)

    return build_world(settings, tmp_path, environment="producao", route=route), route


@dataclass(frozen=True)
class Portal:
    user: User
    contact: ClientContact


def portal_member(world: GuideWorld) -> Portal:
    tenant = world.termo.tenant
    user = add_member(
        tenant, "cliente@pgmei.test", TenantRole.CLIENT_OWNER, client=world.termo.client
    )
    with tenant_context(tenant.pk):
        # PLATFORM_QUERY_OK: test setup reads the seat it just created.
        member = Membership.objects.get(tenant=tenant, user=user)
        contact = ClientContact.objects.create(
            tenant=tenant,
            client=world.termo.client,
            membership=member,
            name="Pessoa MEI",
            email=user.email,
            channel_email_allowed=True,
        )
    return Portal(user, contact)


def session(world: GuideWorld, user: User, *, portal: bool) -> Client:
    host = f"{world.termo.tenant.slug}{'-portal' if portal else ''}.localhost"
    client = Client(SERVER_NAME=host)
    cookie = mfa_session_cookie(user, f"http://{host}/")
    client.cookies[cookie["name"]] = cookie["value"]
    return client


def scan_and_review(world: GuideWorld, link_id: UUID) -> None:
    with patch("apps.cycles.tasks.scanner_from_settings", return_value=FakeScanner()):
        assert scan_quarantine.apply().get() >= 1
    with tenant_context(world.tenant_id):
        link = EvidenceLink.objects.get(pk=link_id)
        intake = DocumentIntake.objects.get(document_id=link.document_id)
    extract_intake(world.tenant_id, intake.pk, now=timezone.now())
    with tenant_context(world.tenant_id):
        intake.refresh_from_db()
        assert intake.state != IntakeState.QUARANTINED
        request = EvidenceRequest.objects.get(pk=link.request_id)
        record_review(
            world.staff,
            link.pk,
            decision=ReviewDecision.ACCEPTED,
            client_reason="",
            corrections=[],
            expected_version=request.version,
        )
        request.refresh_from_db()
        # Accepting the guide never turns the firm-only request client-visible.
        assert request.state == FIRM_ONLY


def publish(
    world: GuideWorld,
    portal: Portal,
    link_id: UUID,
    *,
    occurrence: int = 1,
    supersedes: UUID | None = None,
) -> ActionProposal:
    with tenant_context(world.tenant_id):
        world.cycle.refresh_from_db()
        proposal = propose(
            world.staff,
            world.cycle.pk,
            ProposalDraft(
                ProposalInput(
                    action_kind=ActionKind.CLIENT_PUBLICATION,
                    step_key="publicar-guia-das",
                    occurrence=occurrence,
                    recipient_ids=(portal.contact.pk,),
                    document_link_ids=(link_id,),
                    subject="Guia DAS de outubro",
                    body="A guia DAS do mês está disponível.",
                    supersedes_publication_id=supersedes,
                ),
                timezone.now() + timedelta(days=1),
            ),
            expected_version=world.cycle.version,
        )
        approved = approve(world.owner, proposal.pk, expected_version=1).proposal
        assert execute(
            world.owner, approved.pk, expected_version=approved.version
        ).executed
        return ActionProposal.objects.get(pk=proposal.pk)
