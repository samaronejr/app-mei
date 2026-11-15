"""Clock-slot boundaries, fiscal key namespaces and populated reverse guards."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from importlib import import_module
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest
from django.apps import apps
from django.db.migrations.exceptions import IrreversibleError
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.cycles.scanning import FakeScanner
from apps.integrations.models import ClientProviderLink, ProviderConnection
from tests.fakes.focus import FakeFocus
from tests.fakes.provider import Reply
from tests.integrations.capture_fixtures import (
    START,
    CaptureWorld,
    capture_api,
    capture_world,
    fiscal_rows,
    item,
    key,
    page,
    review_api,
    xml,
)
from tests.obligations.fiscal_fixtures import KEY, nfse

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from pytest_django.fixtures import SettingsWrapper

    from apps.integrations.focus.capture import PreparedPage

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def world(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[CaptureWorld]:
    graph = capture_world(settings, tmp_path, monkeypatch)
    with patch(
        "apps.obligations.fiscal_ingest.scanner_from_settings",
        return_value=FakeScanner(),
    ):
        yield graph


def test_a_new_utc_slot_does_not_shorten_the_one_hour_interval(
    world: CaptureWorld,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, stream = capture_api(), world.stream()
    first = START.replace(minute=59, second=30)
    monkeypatch.setattr(timezone, "now", lambda: first)
    with FakeFocus((page([]), page([]))) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        assert api.synchronize(world.tenant_id, stream.pk) is not None
        monkeypatch.setattr(timezone, "now", lambda: first + timedelta(seconds=31))
        assert api.synchronize(world.tenant_id, stream.pk) is None
        assert len(fake.requests) == 1
        monkeypatch.setattr(timezone, "now", lambda: first + timedelta(hours=1))
        assert api.synchronize(world.tenant_id, stream.pk) is not None
        assert len(fake.requests) == 2


def test_nfse_substitution_references_the_registers_xml_identity(
    world: CaptureWorld,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, stream = capture_api(), world.stream("nfsen_recebidas")
    first = {
        "chave_nfse": key(1),
        "versao": 1,
        "situacao": "autorizado",
        "data_emissao": "2026-09-15T12:30:00-03:00",
    }
    changed = {
        **first,
        "versao": 5,
        "situacao": "substituido",
        "chave_nfse_substituta": key(2),
    }
    original = Reply(
        body=nfse().replace(KEY.encode(), key(1).encode()),
        headers=(("Content-Type", "application/xml"),),
    )
    with FakeFocus((page([first]), original, page([changed]), original)) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        api.synchronize(world.tenant_id, stream.pk)
        before = fiscal_rows(world)
        assert len(before) == 1
        monkeypatch.setattr(timezone, "now", lambda: START + timedelta(hours=1))
        api.synchronize(world.tenant_id, stream.pk)
    after = fiscal_rows(world)
    assert len(after) == 1
    assert after[0].pk == before[0].pk
    assert after[0].fiscal_state == "substituida"
    assert after[0].current_revision is not None
    assert after[0].current_revision.replacement_key == f"NFS{key(2)}"
    assert before[0].current_revision_id != after[0].current_revision_id


@pytest.mark.parametrize("binding", ["company", "credential_generation", "secret_ref"])
def test_page_commit_rechecks_the_binding_that_supplied_its_bytes(
    world: CaptureWorld, settings: SettingsWrapper, binding: str
) -> None:
    api, stream = capture_api(), world.stream()
    prepare = api._prepare_page

    def rebound(*args: object, **kwargs: object) -> PreparedPage:
        prepared = cast("PreparedPage", prepare(*args, **kwargs))
        with tenant_context(world.tenant_id):
            if binding == "secret_ref":
                reference = "focus/rotated"
                ProviderConnection.objects.filter(pk=world.link.connection_id).update(
                    secret_ref=reference
                )
            else:
                changed = (
                    {
                        "provider_company_ref": str(
                            int(world.link.provider_company_ref) + 1
                        )
                    }
                    if binding == "company"
                    else {"credential_generation": world.link.credential_generation + 1}
                )
                ClientProviderLink.objects.filter(pk=world.link.pk).update(**changed)
        return prepared

    with (
        FakeFocus((page([item()]),)) as fake,
        patch.object(api, "_prepare_page", side_effect=rebound),
    ):
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        run = api.synchronize(world.tenant_id, stream.pk)
    assert run is not None
    assert run.state == "bloqueada"
    assert fiscal_rows(world) == []
    assert world.reload(stream).committed_cursor == "0"


def test_a_rebound_stream_does_not_reuse_xml_from_its_previous_binding(
    world: CaptureWorld,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, stream = capture_api(), world.stream()
    prepare = api._prepare_page
    row = item(full=True)

    def rebound(*args: object, **kwargs: object) -> PreparedPage:
        prepared = cast("PreparedPage", prepare(*args, **kwargs))
        with tenant_context(world.tenant_id):
            ClientProviderLink.objects.filter(pk=world.link.pk).update(
                provider_company_ref=str(int(world.link.provider_company_ref) + 1)
            )
        return prepared

    replies = (
        page([row]),
        Reply(body=xml(), headers=(("Content-Type", "application/xml"),)),
        page([{**row, "valor_total": "1800.00"}]),
        Reply(
            body=xml().replace(b"1500.25", b"1800.00"),
            headers=(("Content-Type", "application/xml"),),
        ),
    )
    with FakeFocus(replies) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        with patch.object(api, "_prepare_page", side_effect=rebound):
            first = api.synchronize(world.tenant_id, stream.pk)
        assert first is not None
        assert first.state == "bloqueada"
        assert fiscal_rows(world) == []
        monkeypatch.setattr(timezone, "now", lambda: START + timedelta(hours=1))
        second = api.synchronize(world.tenant_id, stream.pk)
    assert second is not None
    assert second.state == "concluida"
    retained = fiscal_rows(world)
    assert len(retained) == 1
    assert retained[0].total == Decimal("1800.00")
    assert len(fake.requests) == 4


@pytest.mark.parametrize("stale", [False, True])
def test_stale_cursor_refuses_before_register_validation_and_failure_accounting(
    world: CaptureWorld, settings: SettingsWrapper, stale: bool
) -> None:
    api, stream = capture_api(), world.stream()
    prepare = api._prepare_page

    def supersede(*args: object, **kwargs: object) -> PreparedPage:
        prepared = cast("PreparedPage", prepare(*args, **kwargs))
        if stale:
            with tenant_context(world.tenant_id):
                type(stream).objects.filter(pk=stream.pk).update(
                    cursor_version=stream.cursor_version + 1
                )
        return prepared

    # Extraction retains this value; register rejects it at the money boundary.
    # A stale page must be blocked before that refusal can count as a failed run.
    invalid_total = {**item(), "valor_total": "1000000000000.00"}
    with (
        FakeFocus((page([invalid_total]),)) as fake,
        patch.object(api, "_prepare_page", side_effect=supersede),
    ):
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        run = api.synchronize(world.tenant_id, stream.pk)
    assert run is not None
    stream = world.reload(stream)
    expected = ("bloqueada", 0) if stale else ("falhou", 1)
    assert (run.state, stream.consecutive_failures) == expected
    assert run.last_error_code == (
        "execucao_substituida" if stale else "pagina_invalida"
    )
    assert stream.committed_cursor == "0"
    assert fiscal_rows(world) == []


@pytest.mark.parametrize(
    "module",
    [
        "apps.integrations.migrations.0016_capture_sync",
        "apps.cycles.migrations.0023_fiscal_document_review",
    ],
)
def test_empty_capture_and_review_tables_allow_reversal(
    world: CaptureWorld, module: str
) -> None:
    with tenant_context(world.tenant_id):
        assert import_module(module).refuse_reverse_with_rows(apps, None) is None


@pytest.mark.parametrize("record", ["stream", "review"])
def test_populated_capture_and_review_tables_refuse_reversal(
    world: CaptureWorld, record: str
) -> None:
    if record == "stream":
        world.stream()
        module = "apps.integrations.migrations.0016_capture_sync"
    else:
        api = review_api()
        fiscal = world.ingest()
        with tenant_context(world.tenant_id):
            api.record_fiscal_document_review(
                world.setup.graph.actor,
                world.setup.cycle.pk,
                fiscal.pk,
                revision_id=fiscal.current_revision_id,
                decision="aceito",
                input_digest=api.input_digest(fiscal),
                expected_version=world.setup.cycle.version,
            )
        module = "apps.cycles.migrations.0023_fiscal_document_review"
    with (
        tenant_context(world.tenant_id),
        pytest.raises(IrreversibleError, match="forward repair"),
    ):
        import_module(module).refuse_reverse_with_rows(apps, None)
