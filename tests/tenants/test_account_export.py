"""Account archives preserve T18's phase, byte, scope and owned-key boundaries."""

import csv
import io
import zipfile
from datetime import timedelta
from functools import partial
from pathlib import Path

import pytest
from django.core.files import File
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import DatabaseError, connection
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.evidence_zip import build_metadata_archive
from apps.tenants import export_tasks
from apps.tenants.export_service import request_export
from apps.tenants.export_tasks import build_account_export, sweep_account_exports
from apps.tenants.models import (
    AccountExport,
    AccountExportAttempt,
    AccountExportState,
    Membership,
)
from tests.support_threads import run_in_thread_as_runtime
from tests.tenants.test_cancellation_retention import enabled_firm
from tests.ui.factories import Firm, add_client, client_base

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _storage(settings: SettingsWrapper, tmp_path: Path) -> None:
    settings.MEDIA_ROOT = tmp_path


def queued(firm: Firm) -> AccountExport:
    with tenant_context(firm.tenant.pk):
        return request_export(
            firm.owner, firm.tenant.pk, expected_version=firm.tenant.settings_version
        )


def test_account_archive_is_paged_scoped_formula_safe_and_uploaded_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firm = enabled_firm("account-archive")
    other = enabled_firm("account-foreign")
    add_client(firm, legal_name="=1+1", base=client_base(4, "account-archive"))
    export = queued(firm)
    monkeypatch.setattr("apps.tenants.export_service.PAGE_SIZE", 1)
    observed: list[tuple[str, int]] = []
    save = default_storage.save

    def upload(name: str, content: File[bytes], max_length: int | None = None) -> str:
        assert not connection.in_atomic_block
        observed.append(
            run_in_thread_as_runtime(
                firm.tenant.pk,
                lambda: AccountExport.objects.values_list("state", "fence").get(
                    pk=export.pk
                ),
            )
        )
        return save(name, content, max_length=max_length)

    monkeypatch.setattr(default_storage, "save", upload)
    build_account_export(str(firm.tenant.pk), str(export.pk))
    build_account_export(str(firm.tenant.pk), str(export.pk))
    assert observed == [(AccountExportState.BUILDING, 1)]
    with tenant_context(firm.tenant.pk):
        export.refresh_from_db()
        assert export.state == AccountExportState.READY
        assert not AccountExportAttempt.objects.get().cleanup_pending
    with default_storage.open(export.storage_key, "rb") as file:
        payload = file.read()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        rows = list(
            csv.DictReader(io.StringIO(archive.read("clients.csv").decode("utf-8-sig")))
        )
        assert {row["id"] for row in rows} == {
            str(client.pk) for client in firm.clients
        }
        assert "'=1+1" in {row["legal_name"] for row in rows}
        assert other.clients[0].cnpj.encode() not in archive.read("clients.csv")
        assert "storage_key" not in archive.read("documents.csv").decode()
        assert "body_ref" not in archive.read("messages.csv").decode()
        assert "manifest.sha256" in archive.namelist()


def test_account_export_rejects_an_outer_transaction() -> None:
    firm = enabled_firm("account-export-outer")
    export = queued(firm)
    with tenant_context(firm.tenant.pk), pytest.raises(RuntimeError):
        build_account_export(str(firm.tenant.pk), str(export.pk))
    with tenant_context(firm.tenant.pk):
        assert (
            AccountExport.objects.get(pk=export.pk).state == AccountExportState.PENDING
        )


def test_revoked_owner_after_upload_cannot_publish_or_keep_an_orphan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firm = enabled_firm("account-export-revoked")
    export = queued(firm)
    save = default_storage.save
    uploaded: list[str] = []

    def revoke(name: str, content: File[bytes], max_length: int | None = None) -> str:
        key = save(name, content, max_length=max_length)
        uploaded.append(key)
        with tenant_context(firm.tenant.pk):
            Membership.objects.filter(user=firm.owner, tenant=firm.tenant).update(
                is_active=False
            )
        return key

    monkeypatch.setattr(default_storage, "save", revoke)
    build_account_export(str(firm.tenant.pk), str(export.pk))
    with tenant_context(firm.tenant.pk):
        export.refresh_from_db()
        assert (export.state, export.error_code) == (
            AccountExportState.FAILED,
            "authorization_changed",
        )
    assert len(uploaded) == 1
    assert not default_storage.exists(uploaded[0])


def test_a_lost_account_fence_cannot_replace_or_delete_the_winning_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firm = enabled_firm("account-export-stale")
    export = queued(firm)
    winner = default_storage.save("account-exports/winner.zip", ContentFile(b"winner"))
    save = default_storage.save
    losers: list[str] = []

    def supersede(
        name: str, content: File[bytes], max_length: int | None = None
    ) -> str:
        key = save(name, content, max_length=max_length)
        losers.append(key)
        with tenant_context(firm.tenant.pk):
            AccountExport.objects.filter(pk=export.pk).update(
                state=AccountExportState.READY, fence=2, storage_key=winner
            )
        return key

    monkeypatch.setattr(default_storage, "save", supersede)
    build_account_export(str(firm.tenant.pk), str(export.pk))
    with tenant_context(firm.tenant.pk):
        export.refresh_from_db()
        assert (export.state, export.fence, export.storage_key) == (
            AccountExportState.READY,
            2,
            winner,
        )
    assert default_storage.exists(winner)
    assert len(losers) == 1
    assert not default_storage.exists(losers[0])


def test_metadata_byte_cap_fails_closed_without_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firm = enabled_firm("account-export-cap")
    export = queued(firm)
    monkeypatch.setattr(
        export_tasks,
        "build_metadata_archive",
        partial(build_metadata_archive, byte_cap=500),
    )
    build_account_export(str(firm.tenant.pk), str(export.pk))
    with tenant_context(firm.tenant.pk):
        export.refresh_from_db()
        assert (export.state, export.error_code, export.storage_key) == (
            AccountExportState.FAILED,
            "byte_cap",
            "",
        )


def test_database_failure_leaves_a_durable_attempt_for_lease_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firm = enabled_firm("account-export-recover")
    export = queued(firm)

    def unavailable(*_args: object, **_kwargs: object) -> bool:
        raise DatabaseError

    monkeypatch.setattr(export_tasks, "finalize_export", unavailable)
    with pytest.raises(DatabaseError):
        build_account_export(str(firm.tenant.pk), str(export.pk))
    with tenant_context(firm.tenant.pk):
        attempt = AccountExportAttempt.objects.get(export=export)
        assert attempt.cleanup_pending
        assert default_storage.exists(attempt.storage_key)
    later = timezone.now() + timedelta(minutes=16)
    monkeypatch.setattr("apps.tenants.export_tasks.timezone.now", lambda: later)
    sweep_account_exports()
    with tenant_context(firm.tenant.pk):
        attempt.refresh_from_db()
        export.refresh_from_db()
        assert not attempt.cleanup_pending
        assert export.state == AccountExportState.PENDING
        assert export.fence == 2
    assert not default_storage.exists(attempt.storage_key)
