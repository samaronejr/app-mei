"""Cancellation is scheduled, exportable, and still governed by retention classes."""

import io
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from http import HTTPStatus
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.http import StreamingHttpResponse
from django.urls import reverse
from django.utils.timezone import localdate
from pytest_django.fixtures import SettingsWrapper

from apps.audit.models import Event
from apps.core.tenancy import tenant_context
from apps.cycles.retention import place_hold
from apps.cycles.retention_records import ContentPurgeClaim
from apps.cycles.retention_tasks import purge_tenant
from apps.obligations.models import Document
from apps.tenants.export_service import request_export
from apps.tenants.export_tasks import build_account_export
from apps.tenants.lifecycle import purge_cancelled_tenant, schedule_cancellation
from apps.tenants.models import AccountExport, AccountExportState
from apps.tenants.services import TenantConflictError
from tests.cycles.cycle_fixtures import CycleSetup, cycle_setup
from tests.cycles.test_retention_dry_run import old_original
from tests.ui.factories import Firm, client_base, make_cnpj, make_firm
from tests.ui.refusal_contract import assert_readable_page

pytestmark = pytest.mark.django_db(transaction=True)

NOW = datetime(2026, 10, 7, 15, tzinfo=UTC)
TODAY = date(2026, 10, 7)


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apps.tenants.account_state.localdate", lambda: TODAY)
    monkeypatch.setattr(
        "apps.tenants.lifecycle.localdate",
        lambda value=None: localdate(value) if value is not None else TODAY,
    )


def enabled_firm(slug: str) -> Firm:
    firm = make_firm(slug, client_count=1)
    firm.tenant.features = ["cycles_r1", "import_r1", "firm_settings_r1"]
    firm.tenant.save(update_fields=["features"])
    return firm


def cancelled_evidence(slug: str, ends_on: date) -> tuple[CycleSetup, Document]:
    setup = cycle_setup(slug)
    tenant = setup.tenant
    tenant.features = [*tenant.features, "firm_settings_r1"]
    tenant.save(update_fields=["features"])
    with tenant_context(tenant.pk):
        document = old_original(setup)
        schedule_cancellation(
            setup.actor,
            tenant.pk,
            ends_on=ends_on,
            expected_version=tenant.settings_version,
            on=ends_on,
        )
    return setup, document


def test_cancellation_is_scheduled_and_only_becomes_read_only_after_end_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firm = enabled_firm("cancellation-window")
    http = firm.as_owner()
    with tenant_context(firm.tenant.pk):
        scheduled = schedule_cancellation(
            firm.owner,
            firm.tenant.pk,
            ends_on=TODAY,
            expected_version=firm.tenant.settings_version,
            on=TODAY,
        )
        assert scheduled.is_active
    allowed = http.post(
        reverse("client-create"),
        {
            "legal_name": "Ainda no último dia",
            "cnpj": make_cnpj(client_base(2, "cancellation-window")),
        },
    )
    assert allowed.status_code == HTTPStatus.FOUND
    monkeypatch.setattr(
        "apps.tenants.account_state.localdate", lambda: date(2026, 10, 8)
    )
    refused = http.post(
        reverse("client-create"),
        {
            "legal_name": "Após o prazo",
            "cnpj": make_cnpj(client_base(3, "cancellation-window")),
        },
        headers={"HX-Request": "true"},
    )
    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert refused["X-Tenant-Read-Only"] == "1"
    assert http.get(reverse("clients-list")).status_code == HTTPStatus.OK


def test_stale_cancellation_cannot_replace_a_newer_schedule() -> None:
    firm = enabled_firm("cancellation-stale")
    with tenant_context(firm.tenant.pk):
        schedule_cancellation(
            firm.owner,
            firm.tenant.pk,
            ends_on=date(2026, 11, 1),
            expected_version=firm.tenant.settings_version,
            on=TODAY,
        )
        with pytest.raises(TenantConflictError):
            schedule_cancellation(
                firm.owner,
                firm.tenant.pk,
                ends_on=date(2026, 10, 10),
                expected_version=firm.tenant.settings_version,
                on=TODAY,
            )
        firm.tenant.refresh_from_db()
        assert firm.tenant.cancellation_ends_on == date(2026, 11, 1)
        assert Event.objects.filter(action="tenant.cancellation_scheduled").count() == 1


def test_cancellation_cannot_be_backdated() -> None:
    firm = enabled_firm("cancellation-backdated")
    with tenant_context(firm.tenant.pk):
        with pytest.raises(ValidationError):
            schedule_cancellation(
                firm.owner,
                firm.tenant.pk,
                ends_on=date(2026, 10, 6),
                expected_version=firm.tenant.settings_version,
                on=TODAY,
            )
        firm.tenant.refresh_from_db()
        assert firm.tenant.cancellation_ends_on is None
        assert not Event.objects.filter(action="tenant.cancellation_scheduled").exists()


def test_export_runs_after_cancellation_and_stops_at_the_ninety_day_boundary(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.MEDIA_ROOT = tmp_path
    firm = enabled_firm("cancellation-export")
    http = firm.as_owner()
    with tenant_context(firm.tenant.pk):
        tenant = schedule_cancellation(
            firm.owner,
            firm.tenant.pk,
            ends_on=date(2026, 9, 30),
            expected_version=firm.tenant.settings_version,
            on=date(2026, 9, 30),
        )
        export = request_export(
            firm.owner, tenant.pk, expected_version=tenant.settings_version
        )
    build_account_export(str(tenant.pk), str(export.pk))
    with tenant_context(tenant.pk):
        export = AccountExport.objects.get(pk=export.pk)
        assert export.state == AccountExportState.READY
    url = reverse("account-export-download", args=[export.pk])
    monkeypatch.setattr(
        "apps.tenants.account_state.localdate", lambda: date(2026, 12, 29)
    )
    response = http.get(url)
    assert response.status_code == HTTPStatus.OK
    assert isinstance(response, StreamingHttpResponse)
    payload = b"".join(response)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert {
            "clients.csv",
            "engagements.csv",
            "cycles.csv",
            "requests.csv",
            "messages.csv",
            "documents.csv",
        } <= set(archive.namelist())
        assert firm.clients[0].cnpj.encode() in archive.read("clients.csv")
    monkeypatch.setattr(
        "apps.tenants.account_state.localdate", lambda: date(2026, 12, 30)
    )
    assert http.get(url).status_code == HTTPStatus.GONE
    assert http.get(reverse("clients-export-csv")).status_code == HTTPStatus.GONE


def test_an_ended_export_window_is_refused_as_a_readable_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AXR1-054: the admission policy's 410 is a host-neutral page, not bare text."""
    firm = enabled_firm("cancellation-window-refusal")
    http = firm.as_owner()
    with tenant_context(firm.tenant.pk):
        schedule_cancellation(
            firm.owner,
            firm.tenant.pk,
            ends_on=date(2026, 9, 30),
            expected_version=firm.tenant.settings_version,
            on=date(2026, 9, 30),
        )
    monkeypatch.setattr(
        "apps.tenants.account_state.localdate", lambda: date(2026, 12, 30)
    )
    for url in (
        reverse("account-export-download", args=[uuid4()]),
        reverse("clients-export-csv"),
    ):
        body = assert_readable_page(
            http.get(url),
            status=HTTPStatus.GONE,
            reason="O prazo de exportação desta conta encerrou.",
        )
        assert 'href="/"' in body, f"{url}: the refusal offers no way back"


def test_even_the_shared_purge_cannot_erase_the_cancellation_export_window(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup, document = cancelled_evidence(
        "cancellation-window-retained", date(2026, 9, 30)
    )
    counts = purge_tenant(setup.tenant.pk, dry_run=False, now=NOW)
    assert counts["purged"] == 0
    assert counts["retained"] >= 1
    assert default_storage.exists(document.storage_key)
    with tenant_context(setup.tenant.pk):
        assert not ContentPurgeClaim.objects.exists()


def test_after_export_window_dry_run_still_keeps_expired_bytes(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    settings.MEDIA_ROOT = tmp_path
    assert settings.RETENTION_PURGE_DRY_RUN is True
    setup, document = cancelled_evidence("cancellation-dry-run", date(2026, 1, 1))
    counts = purge_cancelled_tenant(setup.tenant.pk, now=NOW)
    assert (counts["eligible"], counts["purged"]) == (1, 0)
    assert default_storage.exists(document.storage_key)
    with tenant_context(setup.tenant.pk):
        document.refresh_from_db()
        assert document.purged_at is None
        assert not ContentPurgeClaim.objects.exists()


def test_explicit_live_purge_keeps_held_content_and_metadata(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    settings.MEDIA_ROOT = tmp_path
    settings.RETENTION_PURGE_DRY_RUN = False
    held, held_document = cancelled_evidence("cancellation-held", date(2026, 1, 1))
    expired, expired_document = cancelled_evidence(
        "cancellation-expired", date(2026, 1, 1)
    )
    with tenant_context(held.tenant.pk):
        place_hold(
            held.actor,
            held.client.pk,
            reason="Evidência em discussão",
            expected_version=held.client.version,
        )
    assert purge_cancelled_tenant(held.tenant.pk, now=NOW)["held"] == 1
    assert purge_cancelled_tenant(expired.tenant.pk, now=NOW)["purged"] == 1
    assert default_storage.exists(held_document.storage_key)
    assert not default_storage.exists(expired_document.storage_key)
    with tenant_context(expired.tenant.pk):
        expired_document.refresh_from_db()
        assert expired_document.purged_at == NOW
        assert expired_document.sha256


def test_cancellation_committed_after_inventory_prevents_the_purge_claim(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = cycle_setup("cancellation-before-claim")
    setup.tenant.features = [*setup.tenant.features, "firm_settings_r1"]
    setup.tenant.save(update_fields=["features"])
    with tenant_context(setup.tenant.pk):
        document = old_original(setup)
    original_context = tenant_context
    visits = 0

    @contextmanager
    def cancel_between_phases(tenant_id: UUID) -> Iterator[UUID]:
        nonlocal visits
        visits += 1
        if visits == 2:
            with original_context(tenant_id):
                schedule_cancellation(
                    setup.actor,
                    tenant_id,
                    ends_on=TODAY,
                    expected_version=setup.tenant.settings_version,
                    on=TODAY,
                )
        with original_context(tenant_id) as active:
            yield active

    monkeypatch.setattr(
        "apps.cycles.retention_tasks.tenant_context", cancel_between_phases
    )
    counts = purge_tenant(setup.tenant.pk, dry_run=False, now=NOW)
    assert visits >= 2
    assert (counts["purged"], counts["retained"]) == (0, 1)
    assert default_storage.exists(document.storage_key)
    with tenant_context(setup.tenant.pk):
        assert not ContentPurgeClaim.objects.exists()


def test_cancellation_cannot_promise_a_window_after_physical_purge_was_claimed(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = cycle_setup("cancellation-after-claim")
    setup.tenant.features = [*setup.tenant.features, "firm_settings_r1"]
    setup.tenant.save(update_fields=["features"])
    with tenant_context(setup.tenant.pk):
        old_original(setup)
    delete = default_storage.delete
    refusals: list[str] = []

    def attempt_cancellation_after_claim(key: str) -> None:
        with tenant_context(setup.tenant.pk), pytest.raises(ValidationError) as refused:
            schedule_cancellation(
                setup.actor,
                setup.tenant.pk,
                ends_on=TODAY,
                expected_version=setup.tenant.settings_version,
                on=TODAY,
            )
        refusals.append(str(refused.value.code))
        delete(key)

    monkeypatch.setattr(default_storage, "delete", attempt_cancellation_after_claim)
    assert purge_tenant(setup.tenant.pk, dry_run=False, now=NOW)["purged"] == 1
    assert refusals == ["cancellation_purge_pending"]
    setup.tenant.refresh_from_db()
    assert setup.tenant.cancellation_ends_on is None
