"""AXRM-019 repair 3 witnesses: the disable reports what it decided under the lock."""

import json
from pathlib import Path

import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.integrations.focus.company import plan_disable, run_disable
from apps.integrations.models import ClientProviderLink
from tests.integrations.test_focus_adapter import World, activate, disable, world
from tests.integrations.test_focus_repair3 import NFE_FLAG, NFSEN_FLAG, _add_nfe

__all__ = ["world"]
db = pytest.mark.django_db(transaction=True)


@db
def test_the_outcome_reports_the_switches_and_streams_decided_under_the_lock(
    world: World, settings: SettingsWrapper
) -> None:
    activate(world)
    stale = plan_disable(world.client.pk)
    _add_nfe(world, settings)
    stored = tuple(sorted(world.reload().streams))
    outcome = run_disable(stale)
    assert outcome.sent == {NFE_FLAG: False, NFSEN_FLAG: False}
    assert outcome.streams == stored


@db
def test_a_whole_link_disable_of_a_link_without_streams_reports_every_stream(
    world: World,
) -> None:
    activate(world)
    with tenant_context(world.tenant.pk):
        ClientProviderLink.objects.filter(pk=world.link.pk).update(streams=[])
    outcome = run_disable(plan_disable(world.client.pk))
    assert outcome.streams == ("nfe_recebidas", "nfsen_recebidas")
    assert outcome.sent == {NFE_FLAG: False, NFSEN_FLAG: False}


@db
def test_the_command_records_and_receipts_what_was_sent(
    world: World,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    activate(world)
    _add_nfe(world, settings)
    stored = sorted(world.reload().streams)
    output = disable(world).splitlines()
    record = json.loads(output[0])
    receipt = json.loads((tmp_path / output[1]).read_text())
    assert record["streams"] == stored
    assert receipt["streams"] == stored
    assert receipt["fields"] == {NFE_FLAG: False, NFSEN_FLAG: False}
