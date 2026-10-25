"""Provider qualification refuses production and writes only minimized evidence."""

import io
import json
import re
from pathlib import Path

import pytest
from django.core.management import call_command, get_commands
from django.core.management.base import CommandError
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.integrations.call_models import FirmProviderCall
from apps.tenants.models import Tenant
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations.serpro_broker_support import ACCESS, JWT, PROCURADOR

pytestmark = pytest.mark.django_db(transaction=True)


def require_command() -> None:
    assert "qualify_provider" in get_commands(), (
        "AXRM-013 must expose the registered qualification command"
    )


def test_fake_termo_scenario_writes_complete_secret_free_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: SettingsWrapper
) -> None:
    # Given a complete fake termo reply and an empty evidence destination.
    require_command()
    response = json.dumps(
        {
            "status": 200,
            "mensagens": [],
            "dados": json.dumps(
                {
                    "autenticar_procurador_token": PROCURADOR,
                    "data_hora_expiracao": "2027-10-25T00:00:00",
                }
            ),
        }
    ).encode()
    stdout, stderr = io.StringIO(), io.StringIO()
    monkeypatch.chdir(tmp_path)
    with FakeSerpro((Reply(body=response),)) as peer:
        settings.SERPRO_FAKE_ORIGIN = peer.origin
        # When the actual management command runs its fake-backed scenario.
        call_command(
            "qualify_provider",
            "serpro",
            "--scenario",
            "termo",
            "--environment",
            "fake",
            stdout=stdout,
            stderr=stderr,
        )
        # Then precisely one allowlisted termo request, not OAuth, reaches the fake.
        assert [(r.method, r.path) for r in peer.requests] == [("POST", "/Apoiar")]
    files = list((tmp_path / ".evidence").glob("AXRM-PQ-serpro-termo-*.txt"))
    assert len(files) == 1
    raw = files[0].read_text()
    evidence = json.loads(raw)
    assert evidence["provider"] == "serpro"
    assert evidence["scenario"] == "termo"
    assert evidence["environment"] == "fake"
    assert evidence["http_status"] == 200
    assert evidence["billing_class"] == "nao_faturada"
    assert evidence["parsed_fields"]["accepted"] is True
    assert evidence["parsed_fields"]["authorization_status"] == "aceito"
    assert evidence["parsed_fields"]["valid_until"] == "2027-10-25"
    assert evidence["elapsed_seconds"] >= 0
    for digest in ("request_digest", "response_digest"):
        assert re.fullmatch("[a-f0-9]{64}", evidence[digest])
    for secret in (ACCESS, JWT, PROCURADOR):
        assert secret not in raw + stdout.getvalue() + stderr.getvalue()
    # The wire request must have its ordinary client-less fenced journal home.
    calls = 0
    for tenant in Tenant.objects.all():
        with tenant_context(tenant.pk):
            calls += FirmProviderCall.objects.filter(
                operation="SP-TERMO", environment="fake", state="sucesso"
            ).count()
    assert calls == 1
    # The synthetic firm is left inactive, so no sweep ever visits it again.
    assert not Tenant.objects.filter(is_active=True).exists()


@pytest.mark.parametrize(
    ("provider", "scenario", "environment"),
    [
        ("serpro", "termo", "producao"),
        ("serpro", "unknown", "fake"),
        ("unknown", "termo", "fake"),
        ("serpro", "termo", "homologacao"),
    ],
)
def test_qualification_refuses_unregistered_or_live_paths_before_any_request(
    provider: str, scenario: str, environment: str, settings: SettingsWrapper
) -> None:
    # Given a request outside the bounded provider/scenario/environment registry.
    require_command()
    with FakeSerpro((Reply(),)) as peer:
        settings.SERPRO_FAKE_ORIGIN = peer.origin
        # When the public CLI is invoked, then refusal precedes every wire request.
        with pytest.raises(CommandError):
            call_command(
                "qualify_provider",
                provider,
                "--scenario",
                scenario,
                "--environment",
                environment,
            )
        assert peer.requests == []
    assert not Tenant.objects.exists()
