"""Production mode end to end against fakes: broker OAuth, termo token and custody."""

import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from email.utils import format_datetime
from hashlib import sha256
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest
from django.apps import apps
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.audit.models import Event
from apps.core.tenancy import tenant_context
from apps.integrations import token_broker, transport
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.call_contracts import (
    AdmissionDeniedError,
    CallRequest,
    CallResult,
)
from apps.integrations.call_models import ProviderCall, ProviderExecutionEpoch
from apps.integrations.call_protocol import claim, perform, reserve
from apps.integrations.models import (
    ClientProviderLink,
    FirmAuthorization,
    ProviderConnection,
)
from apps.integrations.serpro import broker_handoff
from apps.integrations.serpro.adapter import Parties, SerproAdapter, build_request
from apps.integrations.serpro.authorization import TermoSubmission, submit_termo
from apps.integrations.serpro.models import ACTIVE_STATES, FirmIntegrationIssue
from apps.integrations.serpro.qualification import SyntheticTermo, synthetic_termo
from apps.integrations.serpro.termo import TermoInvalidoError
from apps.integrations.serpro.termo_state import termo_head
from apps.integrations.serpro.work import PROC_MISSING, own_firm_condition
from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import JSONValue
from apps.tenants.models import Membership, TenantRole, UsageRecord
from tests.fakes.provider import Received, Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations.serpro_broker_support import (
    ACCESS,
    HMAC_SECRET,
    JWT,
    PROCURADOR,
    RECOVERED_ACCESS,
    REFRESHED_ACCESS,
    BrokerInstance,
    config,
    mutual_tls,
    oauth_reply,
    running_broker,
)
from tests.integrations.serpro_support import FIRM, broker_module

pytestmark = pytest.mark.django_db(transaction=True)

RECOVERED_PROCURADOR = "synthetic-procurador-recovered-013"
OTHER_FIRM = "11444777000161"
OTHER_PROCURADOR = "synthetic-procurador-other-firm-013"
DATA: JSONValue = {"periodoApuracao": "202610"}
DAS_REPLY = Reply(body=b'{"status":200,"mensagens":[],"dados":"{}"}')


def termo_reply(token: str) -> Reply:
    until = timezone.localdate() + timedelta(days=30)
    return Reply(
        body=json.dumps(
            {
                "status": 200,
                "mensagens": [],
                "dados": json.dumps(
                    {
                        "autenticar_procurador_token": token,
                        "data_hora_expiracao": f"{until.isoformat()}T00:00:00",
                    }
                ),
            }
        ).encode()
    )


def headers(request: Received) -> dict[str, str]:
    return {name.lower(): value for name, value in request.headers}


@dataclass
class Production:
    world: SyntheticTermo
    link: ClientProviderLink
    module: ModuleType
    config: object
    broker: BrokerInstance
    sapi: FakeSerpro
    media: Path
    monkeypatch: pytest.MonkeyPatch

    def route(self, origin: str) -> None:
        self.monkeypatch.setitem(transport.ORIGINS, ("serpro", "producao"), origin)

    def use_broker(self, origin: str) -> None:
        self.monkeypatch.setattr(token_broker, "BROKER_URL", f"{origin}/token")
        self.monkeypatch.setattr(broker_handoff, "STORE_URL", f"{origin}/procurador")

    @contextmanager
    def restarted(self) -> Iterator[BrokerInstance]:
        broker = self.module.TokenBroker(self.config)
        with running_broker(self.module, broker) as origin:
            self.use_broker(origin)
            yield broker

    def version(self) -> int:
        with tenant_context(self.world.tenant.pk):
            return ProviderConnection.objects.get(pk=self.world.connection.pk).version

    def submit(self) -> CallResult:
        return submit_termo(
            self.world.actor,
            self.world.tenant.pk,
            TermoSubmission(
                self.world.connection.pk,
                self.world.document.pk,
                self.version(),
                self.world.parties,
            ),
        )

    def call(self, key: str = "das") -> CallResult:
        parties = Parties(
            self.world.parties.contratante,
            self.world.parties.firm,
            self.world.client.cnpj,
        )
        wire = build_request("SP-DAS", parties, DATA)
        identity = sha256(key.encode()).hexdigest()
        request = CallRequest(
            "SP-DAS", identity, sha256(wire.body).hexdigest(), identity
        )
        row = reserve(self.world.tenant.pk, self.link.pk, request)
        ticket = claim(self.world.tenant.pk, row.pk)
        assert ticket is not None
        adapter = SerproAdapter(ProviderTransport("serpro", "producao"), parties, DATA)
        return perform(ticket, adapter)

    def persisted_text(self) -> str:
        rows: list[object] = []
        with tenant_context(self.world.tenant.pk):
            for model in apps.get_app_config("integrations").get_models():
                if model is not ProviderExecutionEpoch:
                    rows.extend(model._default_manager.values())
            rows.extend(Event.objects.values())
            rows.extend(UsageRecord.objects.values())
        files = [
            path.read_bytes().decode("utf-8", "replace")
            for path in self.media.rglob("*")
            if path.is_file()
        ]
        assert any("provider-journal" in str(path) for path in self.media.rglob("*"))
        return repr(rows) + "".join(files)


@pytest.fixture
def production(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Production]:
    media = tmp_path / "media"
    settings.MEDIA_ROOT = media
    settings.PROVIDERS_PAUSED = False
    settings.SERPRO_PAUSED = False
    settings.SERPRO_ENVIRONMENT = "producao"
    settings.PROVIDER_REAL_DATA_APPROVED = True
    settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL = {"serpro": "10.00"}
    settings.USAGE_UNIT_PRICES_BRL = {
        "serpro_consulta": "0.24",
        "serpro_emissao": "0.32",
    }
    monkeypatch.setenv("SERPRO_BROKER_SECRET", HMAC_SECRET)
    world = synthetic_termo("producao")
    with tenant_context(world.tenant.pk):
        ProviderBudget.objects.update(cap_microbrl=2_000_000)
        link = ClientProviderLink.objects.create(
            tenant=world.tenant,
            client=world.client,
            connection=world.connection,
            provider_company_ref="synthetic-production",
            status="ativa",
        )
    certificates = tmp_path / "tls"
    certificates.mkdir()
    tls, _ = mutual_tls(certificates)
    module = broker_module()
    replies = tuple(
        Reply(body=oauth_reply(access))
        for access in (ACCESS, REFRESHED_ACCESS, RECOVERED_ACCESS)
    )
    with FakeSerpro(replies, tls=tls) as sapi:
        settings_for_broker = module.BrokerConfig(**config(certificates, sapi.origin))
        broker = module.TokenBroker(settings_for_broker)
        with running_broker(module, broker) as origin:
            production = Production(
                world,
                link,
                module,
                settings_for_broker,
                broker,
                sapi,
                media,
                monkeypatch,
            )
            production.use_broker(origin)
            yield production


def assert_custody(production: Production, logs: str, *canaries: str) -> None:
    persisted = production.persisted_text()
    for canary in canaries:
        assert canary not in persisted
        assert canary not in logs


def test_production_termo_uses_broker_oauth_and_hands_the_token_to_the_broker(
    production: Production, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with FakeSerpro((termo_reply(PROCURADOR),)) as gateway:
        production.route(gateway.origin)
        # When the firm's signed termo is submitted in production mode.
        assert production.submit().state == "sucesso"
        [request] = gateway.requests
        sent = headers(request)
    # Then SERPRO got the broker's OAuth pair over the wire only, and the
    # procurador token went to broker memory, never to Django persistence or logs.
    assert (sent["authorization"], sent["jwt_token"]) == (f"Bearer {ACCESS}", JWT)
    assert "autenticar_procurador_token" not in sent
    assert production.broker.procurador_for(FIRM) == PROCURADOR
    assert [(r.method, r.path) for r in production.sapi.requests] == [
        ("POST", "/authenticate")
    ]
    assert_custody(production, caplog.text, ACCESS, JWT, PROCURADOR, HMAC_SECRET)


def test_production_client_call_carries_broker_tokens_and_reauthenticates_once(
    production: Production, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with FakeSerpro((termo_reply(PROCURADOR), Reply(status=401), DAS_REPLY)) as gateway:
        production.route(gateway.origin)
        assert production.submit().state == "sucesso"
        # When a paid client call meets one expired-token 401.
        result = production.call()
        first, second = (headers(request) for request in gateway.requests[1:])
    # Then the retry carried a freshly authenticated pair and the same procurador.
    assert result.state == "sucesso"
    assert (
        first["authorization"],
        first["jwt_token"],
        first["autenticar_procurador_token"],
    ) == (f"Bearer {ACCESS}", JWT, PROCURADOR)
    assert (second["authorization"], second["autenticar_procurador_token"]) == (
        f"Bearer {REFRESHED_ACCESS}",
        PROCURADOR,
    )
    assert len(production.sapi.requests) == 2
    assert_custody(
        production,
        caplog.text,
        ACCESS,
        JWT,
        PROCURADOR,
        REFRESHED_ACCESS,
        HMAC_SECRET,
    )


def test_restarted_broker_recovers_the_token_by_resubmitting_the_signed_termo(
    production: Production,
) -> None:
    expires = format_datetime(timezone.now() + timedelta(days=30), usegmt=True)
    not_modified = Reply(
        status=304,
        body=b"",
        headers=(
            ("etag", f'"autenticar_procurador_token:{RECOVERED_PROCURADOR}"'),
            ("expires", expires),
        ),
    )
    with FakeSerpro((termo_reply(PROCURADOR), not_modified, DAS_REPLY)) as gateway:
        production.route(gateway.origin)
        assert production.submit().state == "sucesso"
        version = production.version()
        # When the broker restarts empty and a client call needs the token.
        with production.restarted() as restarted:
            result = production.call()
            held = restarted.procurador_for(FIRM)
        paths = [request.path for request in gateway.requests]
        client = headers(gateway.requests[-1])
    # Then the stored termo was re-submitted once (304 kept it valid) and the
    # client call carried the recovered token without invalidating other calls.
    assert result.state == "sucesso"
    assert paths == ["/Apoiar", "/Apoiar", "/Emitir"]
    assert client["autenticar_procurador_token"] == RECOVERED_PROCURADOR
    assert held == RECOVERED_PROCURADOR
    assert production.version() == version
    with tenant_context(production.world.tenant.pk):
        assert sorted(FirmAuthorization.objects.values_list("status", flat=True)) == [
            "aceito",
            "aceito",
            "enviado",
            "enviado",
        ]
        assert not FirmIntegrationIssue.objects.filter(state__in=ACTIVE_STATES).exists()


def test_refused_recovery_is_firm_work_and_the_client_call_sends_nothing(
    production: Production,
) -> None:
    denial = json.dumps(
        {
            "status": 403,
            "mensagens": [{"codigo": "AcessoNegado-ICGERENCIADOR-016", "texto": "x"}],
            "dados": "{}",
        }
    ).encode()
    with FakeSerpro(
        (termo_reply(PROCURADOR), Reply(status=403, body=denial))
    ) as gateway:
        production.route(gateway.origin)
        assert production.submit().state == "sucesso"
        # When the restarted broker's re-submission of the stored termo is refused.
        with production.restarted():
            result = production.call()
        paths = [request.path for request in gateway.requests]
    # Then the paid call never left; it stays quarantined under todo 11's
    # TransportFailure rule, and the refused termo is owned firm work.
    assert paths == ["/Apoiar", "/Apoiar"]
    assert result.state == "incerta"
    with tenant_context(production.world.tenant.pk):
        head = termo_head(production.world.connection.pk)
        assert head is not None
        assert head.status == "recusado"
        assert list(
            FirmIntegrationIssue.objects.filter(state__in=ACTIVE_STATES).values_list(
                "reason", flat=True
            )
        ) == [PROC_MISSING]


def test_recovery_refused_before_sending_is_owned_firm_work(
    production: Production,
) -> None:
    with FakeSerpro((termo_reply(PROCURADOR), DAS_REPLY)) as gateway:
        production.route(gateway.origin)
        assert production.submit().state == "sucesso"
        # Given the termo's submitter has since lost fiscal.authority.manage.
        with tenant_context(production.world.tenant.pk):
            demoted = Membership.objects.filter(
                tenant=production.world.tenant, user=production.world.actor
            ).update(role=TenantRole.STAFF_ACCOUNTANT)
        assert demoted == 1
        # When the restarted broker needs the stored termo re-submitted.
        with production.restarted():
            result = production.call()
        paths = [request.path for request in gateway.requests]
    # Then nothing more reached SERPRO, and the refusal is owned firm work that
    # names the client call it stopped.
    assert paths == ["/Apoiar"]
    assert result.state == "incerta"
    with tenant_context(production.world.tenant.pk):
        issue = FirmIntegrationIssue.objects.get(state__in=ACTIVE_STATES)
        stopped = ProviderCall.objects.get()
    assert issue.reason == PROC_MISSING
    assert str(stopped.pk) in issue.evidence_refs


def test_client_calls_need_this_connections_termo_in_force(
    production: Production,
) -> None:
    with FakeSerpro((termo_reply(PROCURADOR),)) as gateway:
        production.route(gateway.origin)
        # Given no termo yet, a paid call is refused before any reservation.
        with pytest.raises(AdmissionDeniedError):
            production.call("before-termo")
        assert production.submit().state == "sucesso"
        # When firm work for a missing procurador authorization is open: refused.
        with tenant_context(production.world.tenant.pk):
            own_firm_condition(production.world.connection, PROC_MISSING, [])
        with pytest.raises(AdmissionDeniedError):
            production.call("open-firm-work")
        with tenant_context(production.world.tenant.pk):
            FirmIntegrationIssue.objects.update(state="resolvida")
        # And once the accepted termo's validity has ended: refused.
        later = timezone.localdate() + timedelta(days=31)
        with (
            mock.patch.object(timezone, "localdate", return_value=later),
            pytest.raises(AdmissionDeniedError),
        ):
            production.call("after-expiry")
        # Then only the termo itself ever reached SERPRO.
        assert [request.path for request in gateway.requests] == ["/Apoiar"]


def test_client_call_cannot_use_another_firms_broker_tokens(
    production: Production,
) -> None:
    # Given another firm's procurador token held in the same broker's memory.
    production.broker.store_procurador(OTHER_FIRM, OTHER_PROCURADOR, time.time() + 3600)
    with FakeSerpro((termo_reply(PROCURADOR), DAS_REPLY)) as gateway:
        production.route(gateway.origin)
        # And this tenant's own signed termo, which names its own firm, in force.
        assert production.submit().state == "sucesso"
        # When a caller builds a client call that names the other firm.
        parties = Parties(
            production.world.parties.contratante,
            OTHER_FIRM,
            production.world.client.cnpj,
        )
        wire = build_request("SP-DAS", parties, DATA)
        identity = sha256(b"other-firm").hexdigest()
        row = reserve(
            production.world.tenant.pk,
            production.link.pk,
            CallRequest("SP-DAS", identity, sha256(wire.body).hexdigest(), identity),
        )
        ticket = claim(production.world.tenant.pk, row.pk)
        assert ticket is not None
        result = perform(
            ticket,
            SerproAdapter(ProviderTransport("serpro", "producao"), parties, DATA),
        )
        paths = [request.path for request in gateway.requests]
    # Then nothing left under the other firm's identity or with its token.
    assert paths == ["/Apoiar"]
    assert (result.state, result.provider_code) == ("incerta", "escritorio_divergente")
    assert production.broker.procurador_for(FIRM) == PROCURADOR


def test_termo_cannot_file_a_token_under_a_firm_it_does_not_name(
    production: Production,
) -> None:
    with FakeSerpro((termo_reply(PROCURADOR),)) as gateway:
        production.route(gateway.origin)
        # When a caller submits this tenant's signed termo as another firm.
        with pytest.raises(TermoInvalidoError):
            submit_termo(
                production.world.actor,
                production.world.tenant.pk,
                TermoSubmission(
                    production.world.connection.pk,
                    production.world.document.pk,
                    production.version(),
                    Parties(
                        production.world.parties.contratante, OTHER_FIRM, OTHER_FIRM
                    ),
                ),
            )
        # Then nothing was sent and no firm's broker partition holds a token.
        assert gateway.requests == []
    assert production.broker.procurador_for(OTHER_FIRM) == ""
    assert production.broker.procurador_for(FIRM) == ""
