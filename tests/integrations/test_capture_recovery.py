"""Recovery cannot discard a page or repeatedly spend its bounded XML reads."""

from __future__ import annotations

import json
import multiprocessing
from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from django.db import connection
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.cycles.scanning import FakeScanner
from apps.integrations.serpro.qualification import SCENARIOS
from tests.fakes.focus import FakeFocus
from tests.fakes.provider import Reply
from tests.integrations.capture_fixtures import (
    START,
    capture_api,
    capture_world,
    fiscal_rows,
    item,
    lock_api,
    model,
    page,
    xml,
)
from tests.integrations.capture_process import try_guard

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_django.fixtures import SettingsWrapper

    from tests.integrations.capture_fixtures import CaptureWorld

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def world(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> CaptureWorld:
    return capture_world(settings, tmp_path, monkeypatch)


def test_a_hundred_full_xml_entries_resume_under_the_sixty_request_limit(
    world: CaptureWorld, settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, stream = capture_api(), world.stream()
    rows = [item(index, index=index, full=True) for index in range(1, 101)]
    replies = (
        page(rows),
        *[Reply(body=xml(index)) for index in range(1, 60)],
        page(rows),
        *[Reply(body=xml(index)) for index in range(60, 101)],
        page([]),
    )
    with (
        FakeFocus(replies) as fake,
        patch(
            "apps.obligations.fiscal_ingest.scanner_from_settings",
            return_value=FakeScanner(),
        ),
    ):
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        first = api.synchronize(world.tenant_id, stream.pk)
        assert first.state == "parcial"
        assert len(fake.requests) == 60
        assert world.reload(stream).committed_cursor == "0"
        assert fiscal_rows(world) == []
        monkeypatch.setattr(timezone, "now", lambda: START + timedelta(hours=1))
        second = api.synchronize(world.tenant_id, stream.pk)
    assert second.state == "concluida"
    assert world.reload(stream).committed_cursor == "100"
    assert len(fiscal_rows(world)) == 100
    xml_paths = [
        request.path for request in fake.requests if request.path.endswith(".xml")
    ]
    assert len(xml_paths) == len(set(xml_paths)) == 100
    with tenant_context(world.tenant_id):
        acquisitions = model("CaptureAcquisition").objects.filter(
            stream_id=stream.pk, role="xml_original", state="retida"
        )
        assert acquisitions.count() == 100


def test_scanner_outage_keeps_the_page_unaccounted_and_reuses_the_retained_xml(
    world: CaptureWorld, settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, stream = capture_api(), world.stream()
    replies = (
        page([item(1, full=True)]),
        Reply(body=xml()),
        page([item(1, full=True)]),
    )
    with FakeFocus(replies) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        with patch(
            "apps.obligations.fiscal_ingest.scanner_from_settings",
            return_value=FakeScanner(down=True),
        ):
            api.synchronize(world.tenant_id, stream.pk)
        assert world.reload(stream).committed_cursor == "0"
        assert fiscal_rows(world) == []
        monkeypatch.setattr(timezone, "now", lambda: START + timedelta(hours=1))
        with patch(
            "apps.obligations.fiscal_ingest.scanner_from_settings",
            return_value=FakeScanner(),
        ):
            api.synchronize(world.tenant_id, stream.pk)
    assert world.reload(stream).committed_cursor == "1"
    assert len(fiscal_rows(world)) == 1
    assert (
        len([request for request in fake.requests if request.path.endswith(".xml")])
        == 1
    )


@pytest.mark.parametrize(
    "change",
    ["missing_key", "missing_version", "boolean_version", "invalid_date", "bad_state"],
)
def test_an_unaccountable_item_prevents_every_revision_and_cursor_advance(
    world: CaptureWorld, settings: SettingsWrapper, change: str
) -> None:
    api, stream = capture_api(), world.stream()
    invalid = item(2, index=2)
    if change == "missing_key":
        invalid.pop("chave_nfe")
    elif change == "missing_version":
        invalid.pop("versao")
    elif change == "boolean_version":
        invalid["versao"] = True
    elif change == "invalid_date":
        invalid["data_emissao"] = "not-a-date"
    else:
        invalid["situacao"] = "surprise-state"
    raw = Reply(
        body=json.dumps([item(1), invalid]).encode(),
        headers=(("Content-Type", "application/json"),),
    )
    with FakeFocus((raw,)) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        run = api.synchronize(world.tenant_id, stream.pk)
    assert run.state == "falhou"
    assert world.reload(stream).committed_cursor == "0"
    assert fiscal_rows(world) == []


@pytest.mark.parametrize("collection", ["nfe_recebidas", "nfsen_recebidas"])
def test_capture_qualification_is_registered_for_homologation(
    world: CaptureWorld, collection: str
) -> None:
    scenario = SCENARIOS.get(("focus", collection))
    assert scenario is not None, f"Missing Focus qualification scenario: {collection}"
    assert scenario.environments == frozenset({"fake", "homologacao"})


def test_another_process_cannot_enter_after_the_live_sweeps_database_lock_is_lost(
    world: CaptureWorld, settings: SettingsWrapper
) -> None:
    api = lock_api()
    token = f"synthetic-process-owner-{world.link.pk}"
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    child = context.Process(
        target=try_guard,
        args=(
            token,
            settings.FOCUS_CAPTURE_LOCK_DIR,
            connection.settings_dict.copy(),
            sending,
        ),
    )
    try:
        with api.token_guard(token) as first:
            assert first is not None
            first.connection.close()
            child.start()
            sending.close()
            assert receiving.poll(15), "The child must report at its admission seam."
            assert receiving.recv() is False
            child.join(timeout=15)
            assert child.exitcode == 0
    finally:
        if child.is_alive():
            child.terminate()
            child.join(timeout=15)
        receiving.close()
        sending.close()
    with api.token_guard(token) as recovered:
        assert recovered is not None
        recovered.require_owned()


def test_process_death_releases_both_token_ownership_guards(
    world: CaptureWorld, settings: SettingsWrapper
) -> None:
    api = lock_api()
    token = f"synthetic-process-crash-{world.link.pk}"
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    child = context.Process(
        target=try_guard,
        args=(
            token,
            settings.FOCUS_CAPTURE_LOCK_DIR,
            connection.settings_dict.copy(),
            sending,
        ),
        kwargs={"crash": True},
    )
    try:
        child.start()
        sending.close()
        assert receiving.poll(15), "The child must report holding both guards."
        assert receiving.recv() is True
        child.join(timeout=15)
        assert child.exitcode == 17
        with api.token_guard(token) as recovered:
            assert recovered is not None
            recovered.require_owned()
    finally:
        if child.is_alive():
            child.terminate()
            child.join(timeout=15)
        receiving.close()
        sending.close()
