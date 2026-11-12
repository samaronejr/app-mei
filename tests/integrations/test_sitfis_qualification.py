"""The `sitfis` qualification scenario (todo 13's registry; PQ-SERPRO-1 harness)."""

import io
import json
import re
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest_django.fixtures import SettingsWrapper

from apps.integrations.serpro.qualification import SCENARIOS
from apps.tenants.models import Tenant

pytestmark = pytest.mark.django_db(transaction=True)


def test_the_sitfis_scenario_is_registered_for_fake_and_trial_only() -> None:
    assert ("serpro", "sitfis") in SCENARIOS, "AXRM-016 must register `sitfis`"
    assert SCENARIOS["serpro", "sitfis"].environments == frozenset({"fake", "trial"})


def test_fake_sitfis_scenario_writes_minimized_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: SettingsWrapper
) -> None:
    assert ("serpro", "sitfis") in SCENARIOS, "AXRM-016 must register `sitfis`"
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.SERPRO_FAKE_ORIGIN = ""
    monkeypatch.chdir(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    call_command(
        "qualify_provider",
        "serpro",
        "--scenario",
        "sitfis",
        "--environment",
        "fake",
        stdout=stdout,
        stderr=stderr,
    )
    [path] = (tmp_path / ".evidence").glob("AXRM-PQ-serpro-sitfis-*.txt")
    raw = path.read_text()
    evidence = json.loads(raw)
    assert (evidence["scenario"], evidence["environment"]) == ("sitfis", "fake")
    assert evidence["http_status"] == 200
    assert evidence["billing_class"] == "emissao"
    fields = evidence["parsed_fields"]
    assert fields["state"] == "emitido"
    assert fields["emission_attempts"] == 1
    assert re.fullmatch("[a-f0-9]{64}", fields["document_sha256"])
    assert re.fullmatch("[a-f0-9]{64}", fields["protocol_digest"])
    assert [c["operation"] for c in fields["calls"]] == ["SP-SIT-P", "SP-SIT-R"]
    assert [c["billed"] for c in fields["calls"]] == ["nao", "sim"]
    for digest in ("request_digest", "response_digest"):
        assert re.fullmatch("[a-f0-9]{64}", evidence[digest])
    # Neither the opaque protocol nor report bytes leave the run.
    assert "protocoloRelatorio" not in raw + stdout.getvalue() + stderr.getvalue()
    assert "JVBERi" not in raw
    assert not Tenant.objects.filter(is_active=True).exists()


def test_production_sitfis_is_refused_before_any_firm_exists() -> None:
    with pytest.raises(CommandError):
        call_command(
            "qualify_provider",
            "serpro",
            "--scenario",
            "sitfis",
            "--environment",
            "producao",
        )
    assert not Tenant.objects.exists()
