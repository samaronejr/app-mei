"""Refuse an entire stale Focus page before its first registration write."""

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest
from django.db import connection

from apps.obligations import fiscal_ingest
from apps.obligations.models import FiscalDocument
from tests.fakes.focus import FakeFocus
from tests.fakes.provider import Reply
from tests.integrations import test_capture_sync as capture_tests
from tests.integrations.capture_fixtures import capture_api, national_item, page
from tests.obligations import test_fiscal_documents as direct

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_django.fixtures import SettingsWrapper

    from apps.integrations.focus.capture import PreparedPage
    from tests.integrations.capture_fixtures import CaptureWorld

pytestmark = pytest.mark.django_db(transaction=True)
world = capture_tests.world


@pytest.mark.parametrize("branch", ["head", "historical", "hard-conflict"])
def test_stale_last_item_refuses_before_first_write(
    world: CaptureWorld, settings: SettingsWrapper, branch: str
) -> None:
    api, stream = capture_api(), world.stream("nfsen_recebidas")
    x, xml_x = national_item(1, 7)
    y, xml_y = national_item(2, 8)

    def competing(payload: bytes, version: int = 8) -> None:
        value = direct.ingest(
            world.setup,
            payload=payload,
            version=version,
            source_namespace="focus:nfsen_recebidas",
            client_role="tomador",
            fiscal_state="autorizada",
        )
        assert isinstance(value, FiscalDocument)

    competing(xml_y)
    if branch == "historical":
        competing(xml_y, 9)
    incoming = xml_y.replace(b"<tpRetISSQN>", b"\n<tpRetISSQN>")
    if branch == "hard-conflict":
        incoming = incoming.replace(b">2</tpRetISSQN>", b">3</tpRetISSQN>")
    prepare, commit = api._prepare_page, api._commit_page
    writes: list[str] = []
    attempts = 0
    refused = 0

    def attach(*args: object, **kwargs: object) -> PreparedPage:
        result = cast("PreparedPage", prepare(*args, **kwargs))
        competing(xml_y.replace(b"</tpRetISSQN>", b"</tpRetISSQN>\n"))
        return result

    def trace(
        execute: Callable[..., object],
        sql: str,
        params: object,
        many: bool,
        context: object,
    ) -> object:
        verb = sql.lstrip().split(maxsplit=1)[0].upper()
        if verb in {"INSERT", "UPDATE", "DELETE", "MERGE"}:
            writes.append(sql.split("(", 1)[0][:100])
        return execute(sql, params, many, context)

    def observe(*args: object, **kwargs: object) -> object:
        nonlocal attempts, refused
        attempts += 1
        try:
            with connection.execute_wrapper(trace) if attempts == 1 else nullcontext():
                return commit(*args, **kwargs)
        except fiscal_ingest.StaleISSOriginalsError:
            refused += 1
            raise

    with (
        FakeFocus((page([x, y]), Reply(body=xml_x), Reply(body=incoming))) as fake,
        patch.object(api, "_prepare_page", side_effect=attach),
        patch.object(api, "_commit_page", side_effect=observe),
    ):
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        run = api.synchronize(world.tenant_id, stream.pk)
    assert writes == [], f"First stale page attempted a write: {writes[:2]}"
    assert (attempts, refused) == (2, 1)
    assert run.pages_committed == 1
    assert world.reload(stream).committed_cursor == "8"
    assert len(fake.requests) == 3
