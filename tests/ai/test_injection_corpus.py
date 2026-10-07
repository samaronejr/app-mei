"""A deliberately compromised provider cannot change scope or emit URLs."""

import json
from pathlib import Path

import pytest

from apps.automation.classification import messages_for, schema, validate_output
from apps.automation.dataset import minimal_pdf
from apps.automation.llm import Budget, FakeLLM
from apps.automation.models import AgentRun
from apps.automation.shadow import run_shadow
from apps.automation.types import UnsafeOutputError
from apps.core.tenancy import tenant_context
from tests.cycles.document_fixtures import reviewed
from tests.portal.request_fixtures import PortalSetup

CORPUS = Path(__file__).resolve().parents[1] / "fixtures/eval/injection"
CLIENT = "11222333000181"


@pytest.mark.parametrize("name", ["exfiltrate.txt", "client.txt", "hidden.txt"])
def test_compromised_model_output_is_rejected(name: str) -> None:
    # Given an actual corpus document that the fake is configured to obey.
    text = (CORPUS / name).read_text()
    messages = messages_for(CLIENT, text)
    # When the provider follows the hostile document instead of system instructions.
    reply = FakeLLM(follow_injections=True).complete(schema(), messages, Budget())
    # Then the independent output boundary refuses it.
    with pytest.raises(UnsafeOutputError):
        validate_output(reply.output, CLIENT)


@pytest.mark.parametrize(
    "value",
    [
        "https://attacker.example/leak",
        "//attacker.example/leak",
        "attacker.example",
        "ftp://127.0.0.1/leak",
        "mailto:leak@localhost",
        "file:/etc/passwd",
        "data:text/plain,leak",
        "01991aca-dcee-7000-8000-000000000001",
        "client_01991aca-dcee-7000-8000-000000000001",
        "11444777000161",
        "cliente11444777000161",
        "11.444.777/0001-61",
    ],
)
def test_sensitive_values_are_rejected_in_every_output_position(value: str) -> None:
    for key in ("value", "source_span"):
        field = {"name": "numero", "value": "1", "source_span": "1"}
        field[key] = value
        raw = json.dumps(
            {
                "doc_type": "receipt",
                "fields": [field],
                "client_cnpj_seen": CLIENT,
                "confidence_band": "high",
            }
        )
        with pytest.raises(UnsafeOutputError):
            validate_output(raw, CLIENT)


@pytest.mark.parametrize(
    "raw",
    [
        "null",
        "[]",
        "{}",
        '{"doc_type":"receipt","doc_type":"unknown"}',
        '{"client_id":"01991aca-dcee-7000-8000-000000000001"}',
        '{"tools":[{"name":"fetch"}]}',
    ],
)
def test_malformed_or_extra_schema_properties_are_refused(raw: str) -> None:
    with pytest.raises(UnsafeOutputError):
        validate_output(raw, CLIENT)


@pytest.mark.django_db(transaction=True)
def test_shadow_runner_discards_compromised_output(
    setup: PortalSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    attack = (CORPUS / "exfiltrate.txt").read_text()
    intake, _ = reviewed(setup, "recibo_cliente.pdf", minimal_pdf(attack))
    monkeypatch.setattr(
        "apps.automation.shadow.configured_client",
        lambda tenant_id: FakeLLM(follow_injections=True),
    )
    assert run_shadow(setup.graph.tenant.pk, intake.pk) == "rejected"
    with tenant_context(setup.graph.tenant.pk):
        row = AgentRun.objects.get()
        assert row.output == {}
        assert row.error_code == "unsafe_output"


@pytest.mark.parametrize("value", ["\x00", "\ud800"])
def test_text_rejected_before_postgresql_json_storage(value: str) -> None:
    raw = json.dumps(
        {
            "doc_type": "receipt",
            "client_cnpj_seen": CLIENT,
            "confidence_band": "high",
            "fields": [{"name": "numero", "value": value, "source_span": "1"}],
        }
    )
    with pytest.raises(UnsafeOutputError):
        validate_output(raw, CLIENT)
