"""The `pagtoweb` qualification scenario: real paging on fake, never production."""

import io
import json
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.integrations.serpro.qualification import SCENARIOS

pytestmark = pytest.mark.django_db(transaction=True)


def test_the_scenario_is_registered_for_fake_and_trial_only() -> None:
    scenario = SCENARIOS[("serpro", "pagtoweb")]
    assert scenario.environments == frozenset({"fake", "trial"})


def test_fake_pagtoweb_writes_minimized_evidence_of_two_billed_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    call_command(
        "qualify_provider",
        "serpro",
        "--scenario",
        "pagtoweb",
        "--environment",
        "fake",
        stdout=stdout,
        stderr=stderr,
    )
    [path] = list((tmp_path / ".evidence").glob("AXRM-PQ-serpro-pagtoweb-*.txt"))
    raw = path.read_text()
    evidence = json.loads(raw)
    fields = evidence["parsed_fields"]
    assert (fields["state"], fields["pages_fetched"], fields["payments_found"]) == (
        "encontrado",
        2,
        103,
    )
    assert fields["matched_das"] == 1
    assert [call["billed"] for call in fields["page_calls"]] == ["sim", "sim"]
    assert evidence["billing_class"] == "consulta"
    for secret in ("synthetic-pq-pagtoweb", "Bearer", "PQDAS", "76.6"):
        assert secret not in raw


def test_production_is_refused_before_any_world_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(CommandError):
        call_command(
            "qualify_provider",
            "serpro",
            "--scenario",
            "pagtoweb",
            "--environment",
            "producao",
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
    assert not (tmp_path / ".evidence").exists()
