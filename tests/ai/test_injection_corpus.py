"""A deliberately compromised provider cannot change scope or emit URLs."""

import json
from pathlib import Path

import pytest

from apps.automation.classification import messages_for, schema, validate_output
from apps.automation.dataset import minimal_pdf
from apps.automation.llm import Budget, FakeLLM
from apps.automation.models import AgentRun
from apps.automation.shadow import run_shadow
from apps.automation.types import Result, UnsafeOutputError
from apps.core.tenancy import tenant_context
from tests.ai.fixtures import single_company
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


def classified(value: str) -> str:
    return json.dumps(
        {
            "doc_type": "receipt",
            "fields": [{"name": "numero", "value": value, "source_span": value}],
            "client_cnpj_seen": CLIENT,
            "confidence_band": "high",
        }
    )


@pytest.mark.parametrize(
    "value",
    [
        "attacker.\u0440\u0444",
        "\u4f8b\u5b50.\u6d4b\u8bd5",
        "\u0645\u062b\u0627\u0644.\u0645\u0635\u0631",
        "attacker\u3002example",
        "attacker\uff61example",
        "\u4f8b\u5b50\u3002\u6d4b\u8bd5",
        "\uff48\uff54\uff54\uff50\uff53://attacker.example/leak",
        "\u0440\u0430ypal.com",
        "leak.\u0440\u0444/path",
        "\u2603.net/leak",
    ],
    ids=[
        "cyrillic-tld",
        "chinese",
        "arabic",
        "ideographic-stop",
        "halfwidth-stop",
        "chinese-ideographic-stop",
        "fullwidth-scheme",
        "mixed-script",
        "cyrillic-path",
        "symbol-label",
    ],
)
def test_unicode_web_references_are_rejected_in_every_output_position(
    value: str,
) -> None:
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
    "value",
    [
        "Ltda. ME",
        "S.A.",
        "Sr. Jo\u00e3o da Silva",
        "R$ 1.234,56",
        "11.222.333/0001-81",
        "N\u00ba 123. Total",
        "S\u00e3o Paulo. Recibo",
        "Rua Ita\u00fa, 1.000",
        "09/2026",
        "3.5",
    ],
)
def test_plain_prose_with_dots_is_accepted(value: str) -> None:
    parsed = validate_output(classified(value), CLIENT)
    assert parsed.fields[0].value == value


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "value", ["attacker.\u0440\u0444", "\u4f8b\u5b50\u3002\u6d4b\u8bd5"]
)
def test_shadow_runner_never_persists_a_unicode_web_reference(
    setup: PortalSetup, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    intake = single_company(setup)
    monkeypatch.setattr(
        "apps.automation.shadow.configured_client",
        lambda tenant_id: FakeLLM(reply=Result(classified(value))),
    )
    assert run_shadow(setup.graph.tenant.pk, intake.pk) == "rejected"
    with tenant_context(setup.graph.tenant.pk):
        row = AgentRun.objects.get()
        assert row.output == {}
        assert row.error_code == "unsafe_output"
