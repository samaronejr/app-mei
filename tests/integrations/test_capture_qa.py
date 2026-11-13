"""Synthetic happy and failure drivers whose stdout becomes AXRM-021 receipts."""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.cycles.records import Exception as CycleException
from apps.cycles.scanning import FakeScanner
from apps.obligations.models import FiscalRevision
from tests.fakes.focus import FakeFocus
from tests.fakes.provider import Reply
from tests.integrations.capture_fixtures import (
    START,
    capture_api,
    capture_world,
    fiscal_rows,
    item,
    key,
    page,
    xml,
)
from tests.obligations.fiscal_fixtures import KEY, nfse

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_django.fixtures import SettingsWrapper

pytestmark = pytest.mark.django_db(transaction=True)


def test_happy_two_hundred_thirty_nfe_and_forty_nfse_are_in_the_review_queue(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = capture_api()
    world = capture_world(settings, tmp_path, monkeypatch)
    nfe_stream, nfse_stream = world.stream(), world.stream("nfsen_recebidas")
    nfe_rows = [item(index, index=index) for index in range(1, 231)]
    nfse_rows = [
        {
            "chave_nfse": key(index + 500),
            "versao": index,
            "situacao": "autorizado",
            "data_emissao": "2026-09-15T12:30:00-03:00",
        }
        for index in range(1, 41)
    ]
    replies = (
        page(nfe_rows[:100]),
        page(nfe_rows[100:200]),
        page(nfe_rows[200:]),
        page(nfse_rows),
        *[
            Reply(body=nfse().replace(KEY.encode(), key(index + 500).encode()))
            for index in range(1, 41)
        ],
    )
    with (
        FakeFocus(replies) as fake,
        patch(
            "apps.obligations.fiscal_ingest.scanner_from_settings",
            return_value=FakeScanner(),
        ),
    ):
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        api.synchronize(world.tenant_id, nfe_stream.pk)
        api.synchronize(world.tenant_id, nfse_stream.pk)
    documents = fiscal_rows(world)
    assert len(documents) == 270
    assert sum(document.summary_only for document in documents) == 230
    assert world.reload(nfe_stream).committed_cursor == "230"
    assert world.reload(nfse_stream).committed_cursor == "40"
    response = world.setup.firm().get(reverse("document-queue") + "?fila=fiscais")
    assert response.status_code == 200
    assert response.context is not None
    assert response.context["page"].paginator.count == 270
    with tenant_context(world.tenant_id):
        assert FiscalRevision.objects.count() == 270
    sys.stdout.write(
        "\nAXRM_021_HAPPY="
        + json.dumps(
            {
                "fiscal_documents": 270,
                "revisions": 270,
                "nfe_summaries": 230,
                "nfse_full_xml": 40,
                "nfe_cursor": "230",
                "nfse_cursor": "40",
                "review_queue": 270,
                "provider_requests": len(fake.requests),
                "provider": "synthetic-loopback-only",
            },
            sort_keys=True,
        )
        + "\n"
    )


def test_reset_replays_once_and_three_failures_surface(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = capture_api()
    world = capture_world(settings, tmp_path, monkeypatch)
    stream, failed_stream = world.stream(), world.stream("nfsen_recebidas")
    replies = (
        page([item(1, full=True)]),
        Reply(reset=True),
        page([item(1, full=True)]),
        Reply(body=xml()),
        Reply(reset=True),
        Reply(reset=True),
        Reply(reset=True),
    )
    with (
        FakeFocus(replies) as fake,
        patch(
            "apps.obligations.fiscal_ingest.scanner_from_settings",
            return_value=FakeScanner(),
        ),
    ):
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        api.synchronize(world.tenant_id, stream.pk)
        assert world.reload(stream).committed_cursor == "0"
        assert fiscal_rows(world) == []
        monkeypatch.setattr(timezone, "now", lambda: START + timedelta(hours=1))
        api.synchronize(world.tenant_id, stream.pk)
        assert world.reload(stream).committed_cursor == "1"
        assert len(fiscal_rows(world)) == 1
        for hour in (2, 3, 4):
            monkeypatch.setattr(
                timezone, "now", lambda hour=hour: START + timedelta(hours=hour)
            )
            api.synchronize(world.tenant_id, failed_stream.pk)
    assert world.reload(failed_stream).committed_cursor == "0"
    assert failed_stream.consecutive_failures == 3
    with tenant_context(world.tenant_id):
        assert FiscalRevision.objects.count() == 1
        assert (
            CycleException.objects.filter(reason_code="falha_na_captura").count() == 1
        )
    sys.stdout.write(
        "\nAXRM_021_FAILURE="
        + json.dumps(
            {
                "cursor_after_reset": "0",
                "cursor_after_replay": "1",
                "fiscal_documents": 1,
                "revisions": 1,
                "duplicates": 0,
                "consecutive_failures_other_stream": 3,
                "capture_exceptions": 1,
                "provider": "synthetic-loopback-only",
            },
            sort_keys=True,
        )
        + "\n"
    )
