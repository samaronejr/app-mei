"""A real phased task commits its claim before touching byte storage."""

from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from django.core.files import File
from django.core.files.storage import default_storage
from django.db import DatabaseError, connection
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles import export_tasks
from apps.cycles.evidence_zip import build_archive
from apps.cycles.export_records import EvidenceExport, ExportAttempt, ExportState
from apps.cycles.export_service import finalize_export, request_export
from apps.cycles.export_tasks import build_export
from apps.cycles.models import ServiceCycle
from apps.cycles.services.requests import RequestFields, create_request
from apps.tenants.models import Membership
from tests.cycles.completion_fixtures import completion_setup
from tests.cycles.document_fixtures import reviewed
from tests.portal.request_fixtures import portal_setup
from tests.support_threads import run_in_thread_as_runtime

if TYPE_CHECKING:
    from uuid import UUID

pytestmark = pytest.mark.django_db(transaction=True)


def test_task_claim_is_committed_before_upload(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.MEDIA_ROOT = tmp_path
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    setup = portal_setup("export-committed")
    reviewed(setup, "nfe_emitente_cliente.xml")
    with tenant_context(setup.graph.tenant.pk):
        setup.cycle.refresh_from_db()
        export = request_export(
            setup.graph.actor,
            setup.cycle.pk,
            expected_version=setup.cycle.version,
        )
    observed: list[tuple[str, int, UUID, UUID]] = []
    save = default_storage.save
    open_file = default_storage.open

    def observe() -> None:
        assert not connection.in_atomic_block
        observed.append(
            run_in_thread_as_runtime(
                setup.graph.tenant.pk,
                lambda: EvidenceExport.objects.values_list(
                    "state", "fence", "index_id", "client_id"
                ).get(pk=export.pk),
            )
        )

    def observe_open(name: str, mode: str = "rb") -> File[bytes]:
        observe()
        return open_file(name, mode)

    def observe_save(
        name: str, content: File[bytes], max_length: int | None = None
    ) -> str:
        observe()
        return save(name, content, max_length=max_length)

    monkeypatch.setattr(default_storage, "open", observe_open)
    monkeypatch.setattr(default_storage, "save", observe_save)
    build_export(str(setup.graph.tenant.pk), str(export.pk))
    with tenant_context(setup.graph.tenant.pk):
        export.refresh_from_db()
        assert export.state == ExportState.READY
        assert export.byte_size > 0
        assert len(export.sha256) == 64
        key = export.storage_key
        assert (
            observed
            == [(ExportState.BUILDING, 1, export.index_id, setup.graph.client.pk)] * 2
        )
    assert default_storage.exists(key)
    build_export(str(setup.graph.tenant.pk), str(export.pk))
    assert len(observed) == 2


def test_stale_fence_cannot_finalize_or_delete_winners_key(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = completion_setup("export-stale-fence")
    with tenant_context(setup.graph.tenant.pk):
        export = request_export(
            setup.graph.actor,
            setup.cycle.pk,
            expected_version=setup.cycle.version,
        )
    winner = tmp_path / "exports" / "winner.zip"
    winner.parent.mkdir(parents=True)
    winner.write_bytes(b"winner's distinct completed archive")
    losing: list[str] = []
    save = default_storage.save

    def advance_fence(
        name: str, content: File[bytes], max_length: int | None = None
    ) -> str:
        assert not connection.in_atomic_block
        uploaded = save(name, content, max_length=max_length)
        losing.append(uploaded)
        run_in_thread_as_runtime(
            setup.graph.tenant.pk,
            lambda: EvidenceExport.objects.filter(pk=export.pk).update(
                fence=2,
                state=ExportState.READY,
                storage_key="exports/winner.zip",
                sha256="a" * 64,
                byte_size=winner.stat().st_size,
            ),
        )
        return uploaded

    monkeypatch.setattr(default_storage, "save", advance_fence)
    build_export(str(setup.graph.tenant.pk), str(export.pk))
    with tenant_context(setup.graph.tenant.pk):
        export.refresh_from_db()
        assert export.fence == 2
        assert export.state == ExportState.READY
        assert export.storage_key == "exports/winner.zip"
        assert export.sha256 == "a" * 64
    assert winner.read_bytes() == b"winner's distinct completed archive"
    assert len(losing) == 1
    assert not default_storage.exists(losing[0])


def test_upload_failure_never_marks_an_export_ready(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = completion_setup("export-upload-error")
    with tenant_context(setup.graph.tenant.pk):
        export = request_export(
            setup.graph.actor,
            setup.cycle.pk,
            expected_version=setup.cycle.version,
        )

    def fail_save(
        name: str, content: File[bytes], max_length: int | None = None
    ) -> str:
        assert not connection.in_atomic_block
        message = "synthetic private storage error"
        raise OSError(message)

    monkeypatch.setattr(default_storage, "save", fail_save)
    build_export(str(setup.graph.tenant.pk), str(export.pk))
    with tenant_context(setup.graph.tenant.pk):
        export.refresh_from_db()
        assert export.state == ExportState.FAILED
        assert export.storage_key == ""
        assert export.error_code == "storage_failed"
        assert "synthetic" not in export.error_code


def test_live_source_drift_after_upload_refuses_publication_and_cleans_owned_key(
    settings: SettingsWrapper,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = completion_setup("export-source-drift")
    with tenant_context(setup.graph.tenant.pk):
        export = request_export(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )
    save = default_storage.save
    uploaded: list[str] = []

    def source_changes() -> None:
        cycle = ServiceCycle.objects.get(pk=setup.cycle.pk)
        create_request(
            setup.graph.actor,
            cycle.pk,
            RequestFields("Nova evidência"),
            expected_version=cycle.version,
        )

    def change_after_save(
        name: str,
        content: File[bytes],
        max_length: int | None = None,
    ) -> str:
        uploaded.append(save(name, content, max_length=max_length))
        run_in_thread_as_runtime(setup.graph.tenant.pk, source_changes)
        return uploaded[-1]

    monkeypatch.setattr(default_storage, "save", change_after_save)
    build_export(str(setup.graph.tenant.pk), str(export.pk))
    with tenant_context(setup.graph.tenant.pk):
        export.refresh_from_db()
        assert (export.state, export.error_code, export.storage_key) == (
            ExportState.FAILED,
            "source_changed",
            "",
        )
    assert len(uploaded) == 1
    assert not default_storage.exists(uploaded[0])


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_alternate_storage_name_is_refused_and_actual_attempt_key_is_cleaned(
    settings: SettingsWrapper,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_fails: bool,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = completion_setup("export-alternate")
    with tenant_context(setup.graph.tenant.pk):
        export = request_export(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )
    save = default_storage.save

    def alternate(
        name: str,
        content: File[bytes],
        max_length: int | None = None,
    ) -> str:
        return save(
            name.replace("archive.zip", "alternate.zip"), content, max_length=max_length
        )

    monkeypatch.setattr(default_storage, "save", alternate)
    delete = default_storage.delete

    def delete_refuses(key: str) -> None:
        message = "synthetic cleanup failure"
        raise OSError(message)

    if cleanup_fails:
        monkeypatch.setattr(default_storage, "delete", delete_refuses)
    build_export(str(setup.graph.tenant.pk), str(export.pk))
    with tenant_context(setup.graph.tenant.pk):
        export.refresh_from_db()
        attempt = ExportAttempt.objects.get(export=export)
        assert export.state == ExportState.FAILED
        assert export.error_code == "alternate_storage_name"
        assert attempt.storage_key.endswith("/alternate.zip")
        assert attempt.cleanup_pending is cleanup_fails
        if cleanup_fails:
            ExportAttempt.objects.filter(pk=attempt.pk).update(
                created_at=timezone.now() - timedelta(minutes=16)
            )
    if cleanup_fails:
        assert default_storage.exists(attempt.storage_key)
        monkeypatch.setattr(default_storage, "delete", delete)
        assert export_tasks.sweep_evidence_exports() == 0
        with tenant_context(setup.graph.tenant.pk):
            attempt.refresh_from_db()
            assert not attempt.cleanup_pending
    assert not default_storage.exists(attempt.storage_key)


def test_database_finalize_failure_recovers_fence_and_crash_orphan(
    settings: SettingsWrapper,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = completion_setup("export-finalize-crash")
    with tenant_context(setup.graph.tenant.pk):
        export = request_export(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )
    finalize = finalize_export

    def database_down(*args: object, **kwargs: object) -> bool:
        message = "synthetic database unavailable"
        raise DatabaseError(message)

    monkeypatch.setattr(export_tasks, "finalize_export", database_down)
    with pytest.raises(DatabaseError, match="synthetic database unavailable"):
        build_export(str(setup.graph.tenant.pk), str(export.pk))
    with tenant_context(setup.graph.tenant.pk):
        export.refresh_from_db()
        orphan = ExportAttempt.objects.get(export=export)
        assert (export.state, export.fence, orphan.cleanup_pending) == (
            ExportState.BUILDING,
            1,
            True,
        )
        assert export.lease_until is not None
        after_deadline = export.lease_until + timedelta(seconds=1)
    assert default_storage.exists(orphan.storage_key)
    queued: list[tuple[str, str]] = []
    monkeypatch.setattr(
        build_export, "delay", lambda tenant, pk: queued.append((tenant, pk))
    )
    monkeypatch.setattr(timezone, "now", lambda: after_deadline)
    monkeypatch.setattr(export_tasks, "finalize_export", finalize)
    assert export_tasks.sweep_evidence_exports() == 1
    assert queued == [(str(setup.graph.tenant.pk), str(export.pk))]
    assert not default_storage.exists(orphan.storage_key)
    build_export(str(setup.graph.tenant.pk), str(export.pk))
    with tenant_context(setup.graph.tenant.pk):
        export.refresh_from_db()
        assert (export.state, export.fence) == (ExportState.READY, 3)
        assert export.storage_key != orphan.storage_key
        assert ExportAttempt.objects.filter(export=export).count() == 2
    assert default_storage.exists(export.storage_key)


@pytest.mark.parametrize("phase", ["claim", "finalize"])
def test_revoked_requester_is_denied_before_ready(
    settings: SettingsWrapper,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = completion_setup("export-revoked")
    with tenant_context(setup.graph.tenant.pk):
        export = request_export(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )
    save = default_storage.save
    uploaded: list[str] = []

    def revoke() -> None:
        Membership.objects.filter(
            tenant=setup.graph.tenant, user=setup.graph.actor
        ).update(is_active=False)

    def revoke_after_save(
        name: str,
        content: File[bytes],
        max_length: int | None = None,
    ) -> str:
        uploaded.append(save(name, content, max_length=max_length))
        run_in_thread_as_runtime(setup.graph.tenant.pk, revoke)
        return uploaded[-1]

    monkeypatch.setattr(default_storage, "save", revoke_after_save)
    if phase == "claim":
        run_in_thread_as_runtime(setup.graph.tenant.pk, revoke)
    build_export(str(setup.graph.tenant.pk), str(export.pk))
    with tenant_context(setup.graph.tenant.pk):
        export.refresh_from_db()
        assert (export.state, export.error_code) == (
            ExportState.FAILED,
            "authorization_changed",
        )
        assert not export.storage_key
    assert len(uploaded) == (0 if phase == "claim" else 1)
    assert all(not default_storage.exists(key) for key in uploaded)


def test_expired_lease_cannot_publish_even_without_a_newer_worker(
    settings: SettingsWrapper,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = completion_setup("export-expired-lease")
    with tenant_context(setup.graph.tenant.pk):
        export = request_export(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )
    save = default_storage.save
    uploaded: list[str] = []

    def expire_lease(
        name: str,
        content: File[bytes],
        max_length: int | None = None,
    ) -> str:
        uploaded.append(save(name, content, max_length=max_length))
        run_in_thread_as_runtime(
            setup.graph.tenant.pk,
            lambda: EvidenceExport.objects.filter(pk=export.pk).update(
                lease_until=timezone.now() - timedelta(seconds=1)
            ),
        )
        return uploaded[-1]

    monkeypatch.setattr(default_storage, "save", expire_lease)
    build_export(str(setup.graph.tenant.pk), str(export.pk))
    with tenant_context(setup.graph.tenant.pk):
        export.refresh_from_db()
        assert export.state == ExportState.BUILDING
        assert not export.storage_key
    assert len(uploaded) == 1
    assert not default_storage.exists(uploaded[0])


def test_actual_original_cap_refuses_before_upload_and_preserves_source(
    settings: SettingsWrapper,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    setup = portal_setup("export-actual-cap")
    intake, _ = reviewed(setup, "nfe_emitente_cliente.xml")
    with tenant_context(setup.graph.tenant.pk):
        setup.cycle.refresh_from_db()
        export = request_export(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )
        key = intake.document.storage_key
    oversized = b"x" * 100000
    (tmp_path / key).write_bytes(oversized)
    open_file = default_storage.open
    reads: list[str] = []

    def observe_open(name: str, mode: str = "rb") -> File[bytes]:
        assert not connection.in_atomic_block
        reads.append(name)
        return open_file(name, mode)

    def forbid_upload(*args: object, **kwargs: object) -> str:
        pytest.fail("An oversized original reached storage upload.")

    monkeypatch.setattr(default_storage, "open", observe_open)
    monkeypatch.setattr(default_storage, "save", forbid_upload)
    monkeypatch.setattr(
        export_tasks, "build_archive", partial(build_archive, byte_cap=65536)
    )
    build_export(str(setup.graph.tenant.pk), str(export.pk))
    with tenant_context(setup.graph.tenant.pk):
        export.refresh_from_db()
        assert (export.state, export.error_code) == (ExportState.FAILED, "byte_cap")
        assert export.byte_size == 0
        assert not export.storage_key
    assert reads == [key]
    assert (tmp_path / key).read_bytes() == oversized
