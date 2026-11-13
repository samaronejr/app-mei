"""Fairness, company-token custody and independent sweep ownership."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db import connection
from django.utils import timezone

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.scanning import FakeScanner
from apps.integrations.focus.adapter import secret_variable
from apps.integrations.models import ClientProviderLink, ProviderConnection
from apps.tenants.models import Tenant
from tests.fakes.focus import FakeFocus
from tests.integrations.capture_fixtures import (
    MONTH,
    START,
    capture_api,
    capture_world,
    fiscal_rows,
    item,
    lock_api,
    model,
    page,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from apps.integrations.focus.capture import PreparedPage
    from tests.integrations.capture_fixtures import CaptureWorld

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def world(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> CaptureWorld:
    return capture_world(settings, tmp_path, monkeypatch)


def test_each_tenant_gets_ten_clients_and_five_runs_reach_all_fifty(
    world: CaptureWorld,
) -> None:
    api = capture_api()
    tenants = [world.setup.graph.tenant]
    tenants.extend(
        [
            Tenant.objects.create(
                name=f"Synthetic capture firm {number}",
                slug=f"capture-fair-{number}",
                features=["cycles_r1", "scan_required", "document_capture"],
            )
            for number in (1, 2)
        ]
    )
    expected: dict[UUID, set[UUID]] = {}
    for group, tenant in enumerate(tenants):
        with tenant_context(tenant.pk):
            provider = (
                world.link.connection
                if group == 0
                else ProviderConnection.objects.create(
                    tenant=tenant, provider="focus", environment="fake", status="ativa"
                )
            )
            clients = {world.link.client_id} if group == 0 else set()
            for index in range(49 if group == 0 else 50):
                client = ClientCompany.objects.create(
                    tenant=tenant,
                    legal_name=f"Synthetic client {group}-{index}",
                    cnpj=f"{group + 40:02d}{index:012d}",
                    status="active",
                )
                ClientProviderLink.objects.create(
                    tenant=tenant,
                    client=client,
                    connection=provider,
                    provider_company_ref=str(group * 1000 + index + 1),
                    streams=["nfsen_recebidas"],
                    capture_start_month=MONTH,
                    status="ativa",
                    credential_state="ativa",
                    certificate_cnpj=client.cnpj,
                )
                clients.add(client.pk)
            expected[tenant.pk] = clients
    seen: dict[UUID, set[UUID]] = {tenant.pk: set() for tenant in tenants}
    this_round: dict[UUID, set[UUID]] = {tenant.pk: set() for tenant in tenants}

    def visit(tenant_id: UUID, stream_id: UUID) -> SimpleNamespace:
        with tenant_context(tenant_id):
            stream = model("SyncStream").objects.get(pk=stream_id)
        this_round[tenant_id].add(stream.client_id)
        seen[tenant_id].add(stream.client_id)
        return SimpleNamespace(state="concluida")

    with patch.object(api, "synchronize", side_effect=visit):
        for _ in range(5):
            for visited in this_round.values():
                visited.clear()
            api.sweep()
            assert [len(this_round[tenant.pk]) for tenant in tenants] == [10, 10, 10]
    assert seen == expected


def test_capture_reads_use_the_company_secret_and_never_the_master(
    world: CaptureWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = capture_api()
    world.link.connection.environment = "homologacao"
    master = "synthetic-master-must-not-be-sent"
    company = "synthetic-company-only"
    monkeypatch.setenv(secret_variable(world.link.connection.secret_ref), master)
    reference = api.company_secret_ref(world.link)
    assert world.link.provider_company_ref in reference
    assert "homologacao" in reference
    monkeypatch.setenv(secret_variable(reference), company)
    assert api.company_token(world.link) == company
    monkeypatch.delenv(secret_variable(reference))
    assert api.company_token(world.link) is None, "Never fall back to the reseller."


def test_list_and_xml_adapter_use_the_resolved_company_token(
    world: CaptureWorld, settings: SettingsWrapper
) -> None:
    api, stream = capture_api(), world.stream()
    expected = "synthetic-company-read-only"
    with (
        FakeFocus((page([item()]),)) as fake,
        patch.object(api, "company_token", return_value=expected),
    ):
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        api.synchronize(world.tenant_id, stream.pk)
    headers = dict(fake.requests[0].headers)
    assert headers["Authorization"] == (
        "Basic " + base64.b64encode(f"{expected}:".encode()).decode()
    )


def test_token_guard_uses_its_own_connection_and_releases_normal_and_error_exits(
    world: CaptureWorld,
) -> None:
    api = lock_api()
    token = f"synthetic-lock-{uuid4()}"
    with api.token_guard(token) as first:
        assert first is not None
        assert first.connection is not connection
        first.require_owned()
        raw = first.connection.connection
        assert raw is not None
        with first.connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            first_pid = cursor.fetchone()[0]
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            assert cursor.fetchone()[0] != first_pid
        with api.token_guard(token) as refused:
            assert refused is None
    assert raw.closed
    message = "synthetic interruption"
    with (
        pytest.raises(RuntimeError, match=message),
        api.token_guard(token) as second,
    ):
        raise RuntimeError(message)
    assert second is not None
    assert second.original.closed
    with api.token_guard(token) as third:
        assert third is not None
        third.require_owned()


def test_lost_advisory_connection_keeps_a_live_owner_exclusive_past_lease_expiry(
    world: CaptureWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = lock_api()
    token = f"synthetic-lost-lock-{uuid4()}"
    with api.token_guard(token) as first:
        assert first is not None
        first.connection.close()
        monkeypatch.setattr(timezone, "now", lambda: START + timedelta(days=2))
        with api.token_guard(token) as second:
            assert second is None, (
                "The old live sweep still owns its process guard after PostgreSQL "
                "drops the advisory lock; lease expiry cannot admit another sweep."
            )
        with pytest.raises(api.CaptureLockLostError):
            first.require_owned()
    with api.token_guard(token) as recovered:
        assert recovered is not None
        recovered.require_owned()


def test_losing_the_guard_during_a_page_blocks_both_a_second_sweep_and_the_commit(
    world: CaptureWorld, settings: SettingsWrapper
) -> None:
    api, locks = capture_api(), lock_api()
    first_stream, other_stream = world.stream(), world.stream("nfsen_recebidas")
    prepare, guard_factory = api._prepare_page, locks.token_guard
    current = []
    second_results = []

    @contextmanager
    def retain_guard(token: str) -> Iterator[object]:
        with guard_factory(token) as guard:
            if guard is not None:
                current.append(guard)
            yield guard

    def lose_connection(*args: object, **kwargs: object) -> PreparedPage:
        prepared = cast("PreparedPage", prepare(*args, **kwargs))
        assert len(current) == 1
        current[0].connection.close()
        second_results.append(api.synchronize(world.tenant_id, other_stream.pk))
        return prepared

    with (
        FakeFocus((page([item()]),)) as fake,
        patch.object(api, "token_guard", retain_guard),
        patch.object(api, "_prepare_page", side_effect=lose_connection),
        patch(
            "apps.obligations.fiscal_ingest.scanner_from_settings",
            return_value=FakeScanner(),
        ),
    ):
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        first_result = api.synchronize(world.tenant_id, first_stream.pk)
    assert second_results == [None]
    assert first_result.state == "bloqueada"
    assert len(fake.requests) == 1
    assert world.reload(first_stream).committed_cursor == "0"
    assert world.reload(other_stream).committed_cursor == "0"
    assert fiscal_rows(world) == []


def test_the_sixty_request_quota_is_shared_between_limiter_instances(
    world: CaptureWorld,
) -> None:
    api = lock_api()
    token = f"synthetic-quota-{uuid4()}"
    first, second = api.SharedTokenRateLimiter(), api.SharedTokenRateLimiter()
    assert all(first.acquire(token) for _ in range(30))
    assert all(second.acquire(token) for _ in range(30))
    assert first.acquire(token) is False
    assert second.acquire(token) is False


def test_capture_scheduler_is_hourly_and_uses_a_platform_task(
    world: CaptureWorld, settings: SettingsWrapper
) -> None:
    capture_api()
    from apps.core.tasks import PlatformTask  # noqa: PLC0415
    from apps.integrations.focus.tasks import capture_documents  # noqa: PLC0415

    entry = settings.CELERY_BEAT_SCHEDULE["capture-documents"]
    assert entry["task"] == capture_documents.name
    assert entry["schedule"].run_every.total_seconds() >= 3600
    assert isinstance(capture_documents, PlatformTask)
