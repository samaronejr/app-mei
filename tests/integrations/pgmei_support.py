"""A synthetic MEI firm whose SERPRO termo is in force, for the PGMEI guide (AXRM-017).

Everything here is real: the termo is accepted through the ordinary firm protocol
against a loopback fake, the cycle is opened from the installed `mei-mensal` pack and
the guide is requested through the public service. No provider is ever reached.
"""

import base64
import importlib
import importlib.util
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientAssignment, Engagement, TaxProfile
from apps.core.tenancy import tenant_context
from apps.cycles.models import PlaybookVersion, ServiceCycle
from apps.cycles.packs import install_system_packs
from apps.cycles.services.cycle import open_cycle
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.models import ClientProviderLink
from apps.integrations.serpro.authorization import TermoSubmission, submit_termo
from apps.integrations.serpro.qualification import SyntheticTermo, synthetic_termo
from apps.obligations.models import Obligation
from apps.tenants.models import Membership, TenantRole
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro


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
    def tenant_id(self):  # noqa: ANN201 - UUID, kept short for the tests
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
def guide_world(settings: SettingsWrapper, tmp_path: Path) -> Iterator[GuideWorld]:
    return build_world(settings, tmp_path)


@pytest.fixture
def production_world(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[GuideWorld, Callable[[str], None]]]:
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
