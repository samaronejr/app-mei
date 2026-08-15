"""Fault injection for document bytes and their tenant-scoped metadata rows."""

import hashlib
import importlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from io import BytesIO, StringIO
from pathlib import Path
from unittest import mock

import pytest
from django.core.files import File
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import current_tenant_id, tenant_context
from apps.obligations.models import Document
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

PORTAL_HOST = "acme-portal.localhost"
PORTAL_URLCONF = "apps.portal.urls"
PASSWORD = "sufficiently-long-passphrase"  # noqa: S105
UPLOAD_PAYLOAD = b"new fiscal evidence"
EXISTING_PAYLOAD = b"pre-existing fiscal evidence"
REQUESTED_KEY = "pre-existing-requested-key"
RECONCILE_MODULE = "apps.core.management.commands.reconcile_documents"
RECONCILE_COMMAND = "reconcile_documents"
RECONCILE_CNPJS = {
    "alpha": "11222333000181",
    "beta": "11444777000161",
}
RECONCILE_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "apps"
    / "core"
    / "management"
    / "commands"
    / "reconcile_documents.py"
)


@dataclass(frozen=True, slots=True)
class UploadPortal:
    """The smallest real portal identity that can exercise the upload endpoint."""

    tenant: Tenant
    client: ClientCompany
    user: User
    http: Client
    storage: FileSystemStorage


@pytest.fixture
def upload_portal(
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> UploadPortal:
    """Build one client-role session and isolate its object-storage boundary."""
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]
    monkeypatch.setattr("apps.portal.middleware.PORTAL_URLCONF", PORTAL_URLCONF)

    storage = FileSystemStorage(location=tmp_path)
    monkeypatch.setattr("apps.portal.views.default_storage", storage)

    tenant = Tenant.objects.create(name="Acme", slug="acme")
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding under the tenant context the portal will use.
        client = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="CLIENTE ALPHA",
            cnpj="11222333000181",
            is_mei=True,
        )
    user = User.objects.create_user(email="dono@mei.example", password=PASSWORD)
    enrol_totp(user)
    Membership.objects.create(
        user=user,
        tenant=tenant,
        role=TenantRole.CLIENT_OWNER,
        client=client,
    )
    http = Client()
    http.force_login(user)
    return UploadPortal(tenant, client, user, http, storage)


def _upload(portal: UploadPortal) -> None:
    """POST one valid file through the real portal route."""
    portal.http.post(
        "/documentos/enviar",
        {
            "file": SimpleUploadedFile(
                "evidence.pdf",
                UPLOAD_PAYLOAD,
                content_type="application/pdf",
            ),
        },
        headers={"host": PORTAL_HOST},
    )


def _document_row(
    portal: UploadPortal,
    *,
    payload: bytes = EXISTING_PAYLOAD,
    storage_key: str = REQUESTED_KEY,
) -> Document:
    """Create metadata for one object without coupling storage to model persistence."""
    document = Document(
        tenant=portal.tenant,
        client=portal.client,
        sha256=hashlib.sha256(payload).hexdigest(),
        original_filename="existing.pdf",
        content_type="application/pdf",
        byte_size=len(payload),
        uploaded_by=portal.user,
        storage_key=storage_key,
    )
    with tenant_context(portal.tenant.id):
        document.save(force_insert=True)
    return document


@dataclass(frozen=True, slots=True)
class ReconcileTenant:
    """One tenant, one client, and one document known to reconciliation."""

    tenant: Tenant
    client: ClientCompany
    document: Document


def _patch_reconcile_storage(
    monkeypatch: pytest.MonkeyPatch,
    storage: FileSystemStorage,
) -> None:
    """Install a filesystem-backed bucket at the command's storage boundary."""
    command_module = importlib.import_module(RECONCILE_MODULE)
    monkeypatch.setattr(command_module, "default_storage", storage)


def _reconcile_tenant(
    *,
    user: User,
    storage: FileSystemStorage,
    name: str,
    storage_key: str,
    payload: bytes,
) -> ReconcileTenant:
    """Create one tenant-scoped row and its matching object."""
    slug = name.casefold()
    tenant = Tenant.objects.create(name=name, slug=slug)
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding under the exact tenant context being tested.
        client = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name=f"CLIENTE {name}",
            cnpj=RECONCILE_CNPJS[slug],
            is_mei=True,
        )
        document = Document(
            tenant=tenant,
            client=client,
            sha256=hashlib.sha256(payload).hexdigest(),
            original_filename=f"{slug}.pdf",
            content_type="application/pdf",
            byte_size=len(payload),
            uploaded_by=user,
            storage_key=storage_key,
        )
        document.save(force_insert=True)
    stored_name = storage.save(storage_key, File(BytesIO(payload)))
    assert stored_name == storage_key
    return ReconcileTenant(tenant, client, document)


def _reconcile_user() -> User:
    """Create the uploader required by document metadata without portal setup."""
    return User.objects.create_user(email="reconcile@example.com")


def test_object_write_failure_creates_no_metadata_row(
    upload_portal: UploadPortal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A storage failure remains the surfaced error and never reaches the INSERT."""
    error = OSError("injected object write failure")
    monkeypatch.setattr(upload_portal.storage, "save", mock.Mock(side_effect=error))

    with pytest.raises(OSError, match="injected object write failure"):
        _upload(upload_portal)

    with tenant_context(upload_portal.tenant.id):
        assert not Document.objects.exists()


def test_row_save_failure_deletes_the_object_written_by_this_request(
    upload_portal: UploadPortal,
) -> None:
    """A failed INSERT must compensate only the storage write that preceded it."""
    with (
        mock.patch.object(
            upload_portal.storage,
            "delete",
            wraps=upload_portal.storage.delete,
        ) as deleted,
        mock.patch.object(
            Document,
            "save",
            side_effect=IntegrityError("injected row failure"),
        ),
        pytest.raises(IntegrityError, match="injected row failure"),
    ):
        _upload(upload_portal)

    assert deleted.call_count == 1
    _directories, files = upload_portal.storage.listdir("")
    assert files == []
    with tenant_context(upload_portal.tenant.id):
        assert not Document.objects.exists()


def test_cleanup_failure_is_captured_without_masking_the_row_error(
    upload_portal: UploadPortal,
) -> None:
    """A failed compensation is observable while the original INSERT error survives."""
    cleanup_error = OSError("injected cleanup failure")
    with (
        mock.patch.object(
            upload_portal.storage,
            "delete",
            side_effect=cleanup_error,
        ),
        mock.patch.object(
            Document,
            "save",
            side_effect=IntegrityError("injected row failure"),
        ),
        mock.patch("sentry_sdk.capture_exception") as captured,
        pytest.raises(IntegrityError, match="injected row failure"),
    ):
        _upload(upload_portal)

    captured.assert_called_once_with(cleanup_error)
    _directories, files = upload_portal.storage.listdir("")
    assert len(files) == 1
    with tenant_context(upload_portal.tenant.id):
        assert not Document.objects.exists()


def test_storage_selected_alternative_name_is_deleted_and_rejected(
    upload_portal: UploadPortal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A collision suffix cannot leave a row pointing at the requested object key."""
    original_save = upload_portal.storage.save
    alternate_names: list[str] = []

    def save_under_alternate(name: str, content: File[bytes]) -> str:
        alternate = f"{name}_alternate"
        alternate_names.append(alternate)
        return original_save(alternate, content)

    monkeypatch.setattr(upload_portal.storage, "save", save_under_alternate)

    with pytest.raises(
        RuntimeError, match="storage changed the requested document key"
    ):
        _upload(upload_portal)

    assert len(alternate_names) == 1
    assert not upload_portal.storage.exists(alternate_names[0])
    with tenant_context(upload_portal.tenant.id):
        assert not Document.objects.exists()


def test_collision_deletes_only_alternate_and_preserves_requested_object_and_row(
    upload_portal: UploadPortal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Collision cleanup must never touch the existing document's row or bytes."""
    upload_portal.storage.save(
        REQUESTED_KEY, File(__import__("io").BytesIO(EXISTING_PAYLOAD))
    )
    existing = _document_row(upload_portal)
    original_row = tuple(
        (field.attname, getattr(existing, field.attname))
        for field in existing._meta.concrete_fields
    )
    storage_key_field = Document._meta.get_field("storage_key")
    monkeypatch.setattr(storage_key_field, "default", REQUESTED_KEY)

    alternate = f"{REQUESTED_KEY}_alternate"
    original_save = upload_portal.storage.save

    def save_under_alternate(_name: str, content: File[bytes]) -> str:
        return original_save(alternate, content)

    monkeypatch.setattr(upload_portal.storage, "save", save_under_alternate)
    with (
        mock.patch.object(
            upload_portal.storage,
            "delete",
            wraps=upload_portal.storage.delete,
        ) as deleted,
        pytest.raises(RuntimeError, match="storage changed the requested document key"),
    ):
        _upload(upload_portal)

    deleted.assert_called_once_with(alternate)
    assert upload_portal.storage.exists(REQUESTED_KEY)
    assert not upload_portal.storage.exists(alternate)
    with upload_portal.storage.open(REQUESTED_KEY, "rb") as stored:
        assert stored.read() == EXISTING_PAYLOAD
    with tenant_context(upload_portal.tenant.id):
        existing.refresh_from_db()
        current_row = tuple(
            (field.attname, getattr(existing, field.attname))
            for field in existing._meta.concrete_fields
        )
        assert current_row == original_row
        assert Document.objects.count() == 1


def test_missing_object_download_returns_503_captures_sentry_and_hides_stack(
    upload_portal: UploadPortal,
) -> None:
    """An authorised row with missing bytes produces a bounded safe response."""
    document = _document_row(upload_portal, storage_key="missing-object-key")
    missing = FileNotFoundError("MISSING_OBJECT_CANARY /private/bucket/key")

    with (
        mock.patch.object(
            upload_portal.storage, "size", return_value=document.byte_size
        ),
        mock.patch.object(upload_portal.storage, "open", side_effect=missing),
        mock.patch("sentry_sdk.capture_exception") as captured,
    ):
        response = upload_portal.http.get(
            f"/documentos/{document.storage_key}",
            headers={"host": PORTAL_HOST},
        )

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    captured.assert_called_once_with(missing)
    body = response.content.decode()
    assert "MISSING_OBJECT_CANARY" not in body
    assert "/private/bucket/key" not in body
    assert "Traceback" not in body


def test_reconcile_reports_orphan_age_without_deleting_any_object(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Phase two reports only keys absent from the completed global row union."""
    storage = FileSystemStorage(location=tmp_path)
    _patch_reconcile_storage(monkeypatch, storage)
    user = _reconcile_user()
    valid = _reconcile_tenant(
        user=user,
        storage=storage,
        name="Alpha",
        storage_key="valid-alpha-key",
        payload=b"alpha evidence",
    )
    orphan_key = storage.save("orphan-key", File(BytesIO(b"orphan bytes")))
    storage.save("_probe/ignored", File(BytesIO(b"probe bytes")))
    fixed_now = datetime(2026, 8, 10, 12, tzinfo=UTC)
    monkeypatch.setattr("django.utils.timezone.now", lambda: fixed_now)
    monkeypatch.setattr(
        storage,
        "get_modified_time",
        mock.Mock(return_value=fixed_now - timedelta(days=2)),
    )
    stdout = StringIO()

    with (
        mock.patch.object(storage, "listdir", wraps=storage.listdir) as listed,
        mock.patch.object(storage, "delete", wraps=storage.delete) as deleted,
    ):
        call_command(RECONCILE_COMMAND, stdout=stdout)

    listed.assert_called_once_with("")
    assert deleted.call_count == 0
    assert storage.exists(valid.document.storage_key)
    assert storage.exists(orphan_key)
    assert stdout.getvalue().splitlines() == [
        "PHASE 1 tenants=1 documents=1 missing=0 hashes_checked=1 hash_mismatches=0",
        "PHASE 2 bucket_keys=2 orphan_candidates=1",
        "ORPHAN storage_key=orphan-key age_seconds=172800",
        "COMPLETE report-only",
    ]


def test_reconcile_reports_row_without_object_as_critical(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Every tenant row is existence-checked even when no hash sample can be read."""
    storage = FileSystemStorage(location=tmp_path)
    _patch_reconcile_storage(monkeypatch, storage)
    missing = _reconcile_tenant(
        user=_reconcile_user(),
        storage=storage,
        name="Alpha",
        storage_key="missing-alpha-key",
        payload=b"missing alpha evidence",
    )
    storage.delete(missing.document.storage_key)
    stdout = StringIO()

    call_command(RECONCILE_COMMAND, stdout=stdout)

    output = stdout.getvalue()
    assert (
        f"CRITICAL row-without-object tenant={missing.tenant.id} "
        "storage_key=missing-alpha-key"
    ) in output
    assert "missing=1" in output
    assert "ORPHAN" not in output


def test_reconcile_hash_sample_is_bounded_and_reports_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The public sample-size option bounds authenticated object reads per tenant."""
    storage = FileSystemStorage(location=tmp_path)
    _patch_reconcile_storage(monkeypatch, storage)
    user = _reconcile_user()
    sampled = _reconcile_tenant(
        user=user,
        storage=storage,
        name="Alpha",
        storage_key="a-sampled-key",
        payload=b"expected alpha evidence",
    )
    _reconcile_tenant(
        user=user,
        storage=storage,
        name="Beta",
        storage_key="b-unsampled-key",
        payload=b"beta evidence",
    )
    with storage.open(sampled.document.storage_key, "wb") as stored:
        stored.write(b"corrupted bytes")
    stdout = StringIO()

    with mock.patch.object(storage, "open", wraps=storage.open) as opened:
        call_command(RECONCILE_COMMAND, hash_sample_size=1, stdout=stdout)

    assert opened.call_count == 2
    output = stdout.getvalue()
    assert "hashes_checked=2 hash_mismatches=1" in output
    assert (
        f"CRITICAL sha256-mismatch tenant={sampled.tenant.id} storage_key=a-sampled-key"
    ) in output


def test_reconcile_two_tenants_are_isolated_but_share_one_global_union(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Each phase-one context sees itself while phase two protects both tenants."""
    storage = FileSystemStorage(location=tmp_path)
    _patch_reconcile_storage(monkeypatch, storage)
    user = _reconcile_user()
    alpha = _reconcile_tenant(
        user=user,
        storage=storage,
        name="Alpha",
        storage_key="alpha-valid-key",
        payload=b"alpha evidence",
    )
    beta = _reconcile_tenant(
        user=user,
        storage=storage,
        name="Beta",
        storage_key="beta-valid-key",
        payload=b"beta evidence",
    )
    real_exists = storage.exists
    observed: list[tuple[object, str]] = []

    def observe_exists(name: str) -> bool:
        observed.append((current_tenant_id.get(), name))
        return real_exists(name)

    monkeypatch.setattr(storage, "exists", observe_exists)
    stdout = StringIO()
    with mock.patch.object(storage, "listdir", wraps=storage.listdir) as listed:
        call_command(RECONCILE_COMMAND, hash_sample_size=0, stdout=stdout)

    assert observed == [
        (alpha.tenant.id, alpha.document.storage_key),
        (beta.tenant.id, beta.document.storage_key),
    ]
    listed.assert_called_once_with("")
    output = stdout.getvalue()
    assert "PHASE 1 tenants=2 documents=2" in output
    assert f"ORPHAN storage_key={beta.document.storage_key}" not in output
    assert "orphan_candidates=0" in output


def test_reconcile_tenant_failure_aborts_before_any_orphan_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An incomplete tenant union cannot reach the bucket-comparison phase."""
    storage = FileSystemStorage(location=tmp_path)
    _patch_reconcile_storage(monkeypatch, storage)
    user = _reconcile_user()
    _reconcile_tenant(
        user=user,
        storage=storage,
        name="Alpha",
        storage_key="alpha-valid-key",
        payload=b"alpha evidence",
    )
    beta = _reconcile_tenant(
        user=user,
        storage=storage,
        name="Beta",
        storage_key="beta-valid-key",
        payload=b"beta evidence",
    )
    real_exists = storage.exists

    def fail_during_beta(name: str) -> bool:
        if current_tenant_id.get() == beta.tenant.id:
            msg = "injected tenant iteration failure"
            raise OSError(msg)
        return real_exists(name)

    monkeypatch.setattr(storage, "exists", fail_during_beta)
    stdout = StringIO()
    with (
        mock.patch.object(storage, "listdir", wraps=storage.listdir) as listed,
        pytest.raises(CommandError, match="orphan report suppressed"),
    ):
        call_command(RECONCILE_COMMAND, hash_sample_size=0, stdout=stdout)

    assert listed.call_count == 0
    assert "ORPHAN" not in stdout.getvalue()


def test_reconcile_processed_tenant_count_mismatch_aborts_before_bucket_listing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A shortened tenant iteration cannot masquerade as a complete global union."""
    storage = FileSystemStorage(location=tmp_path)
    _patch_reconcile_storage(monkeypatch, storage)
    _reconcile_tenant(
        user=_reconcile_user(),
        storage=storage,
        name="Alpha",
        storage_key="alpha-valid-key",
        payload=b"alpha evidence",
    )
    stdout = StringIO()

    with (
        mock.patch.object(Tenant.objects, "count", return_value=2),
        mock.patch.object(storage, "listdir", wraps=storage.listdir) as listed,
        pytest.raises(CommandError, match="processed 1 of 2 tenants"),
    ):
        call_command(RECONCILE_COMMAND, hash_sample_size=0, stdout=stdout)

    assert listed.call_count == 0
    assert "ORPHAN" not in stdout.getvalue()


def test_reconcile_empty_union_with_nonempty_bucket_aborts_without_orphan_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The sweep-empty guard refuses to classify a populated bucket from zero rows."""
    storage = FileSystemStorage(location=tmp_path)
    _patch_reconcile_storage(monkeypatch, storage)
    storage.save("unclaimed-key", File(BytesIO(b"unclaimed")))
    stdout = StringIO()

    with pytest.raises(CommandError, match="empty document union"):
        call_command(RECONCILE_COMMAND, stdout=stdout)

    assert "ORPHAN" not in stdout.getvalue()


def test_reconcile_command_has_no_destructive_storage_path() -> None:
    """The report-only command exposes neither a deletion call nor a delete flag."""
    source = RECONCILE_SOURCE.read_text(encoding="utf-8")
    assert ".delete(" not in source
    assert "--delete-orphans" not in source
