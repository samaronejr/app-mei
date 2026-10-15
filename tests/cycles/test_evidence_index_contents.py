"""Validate the closed schema and every proof relationship from real scenario A."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.models import (
    Approval,
    ClientAcknowledgement,
    CycleEvent,
    EvidenceLink,
    Review,
)
from apps.delivery.models import DeliveryAttempt
from tests.cycles.evidence_fixtures import scenario_a

pytestmark = pytest.mark.django_db(transaction=True)

SCHEMA = (
    Path(__file__).resolve().parents[2] / "apps/cycles/schemas/evidence_index_v1.json"
)


def test_scenario_a_index_schema_and_complete_proof_identities(
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    scenario = scenario_a(settings, tmp_path, "evidence-schema-a")
    schema = json.loads(SCHEMA.read_text())
    Draft202012Validator.check_schema(schema)
    data = scenario.index.data
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(data)
    assert data["generation"] == {
        "id": str(scenario.index.pk),
        "generated_at": scenario.index.created_at.isoformat(),
        "amendment": scenario.index.amendment,
        "cycle_revision": scenario.index.cycle_revision,
    }
    cycle = scenario.setup.cycle
    with tenant_context(scenario.setup.graph.tenant.pk):
        for section, rows in (
            ("links", EvidenceLink.objects.filter(request__cycle=cycle)),
            ("reviews", Review.objects.filter(link__request__cycle=cycle)),
            ("approvals", Approval.objects.filter(proposal__cycle=cycle)),
            (
                "provider_responses",
                DeliveryAttempt.objects.filter(message__cycle=cycle),
            ),
            (
                "acknowledgements",
                ClientAcknowledgement.objects.filter(publication__cycle=cycle),
            ),
        ):
            expected = {str(pk) for pk in rows.values_list("pk", flat=True)}
            assert expected, section
            assert {row["id"] for row in data[section]} == expected
        request_events = CycleEvent.objects.filter(
            cycle=cycle, aggregate_id=scenario.setup.request.pk
        )
        assert set(request_events.values_list("aggregate_version", flat=True)) == set(
            scenario.request_versions
        )
        assert {str(pk) for pk in request_events.values_list("pk", flat=True)} <= {
            row["id"] for row in data["events"]
        }
        link = EvidenceLink.objects.select_related("document").get(request__cycle=cycle)
        assert data["documents"][0]["sha256"] == link.document.sha256
        approval_digests = set(
            Approval.objects.filter(proposal__cycle=cycle).values_list(
                "digest", flat=True
            )
        )
        assert {row["digest"] for row in data["approvals"]} == approval_digests
    serialized = json.dumps(data)
    assert "PRIVATE-SCENARIO-A-INSTRUCTIONS" not in serialized
    assert "OTHER-CLIENT-CANARY" not in serialized
    assert "Confira a nota do período." not in serialized
    assert data["cycle"]["client_id"] == str(scenario.setup.graph.client.pk)
