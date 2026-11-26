"""A federal copy is disposed of only when its disposal is proved (AXRM-051 B-1).

These tests drive the real purge, scanner and writer boundaries with a SITFIS report:
a claim completes only when the stored bytes were the original's single copy and
are gone afterwards; storage support is an allow-list; the scanner never writes a
disposed original back nor drops a held federal copy; and writers that would add a
use refuse once a disposal claim exists, also across tenants.
"""

import hashlib
import os
import posixpath
import stat
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import psycopg
import psycopg.sql
import pytest
from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation, ValidationError
from django.core.files import File
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, Storage, default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.models import F
from django.db.transaction import TransactionManagementError
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper
from storages.backends.s3 import S3Storage
from storages.utils import clean_name

from apps.audit.models import Event
from apps.core.tenancy import tenant_context
from apps.cycles import intake as intake_module
from apps.cycles import retention, retention_tasks, scanning
from apps.cycles.evidence import (
    CLEAN_STATES,
    PENDING_SCAN_STATES,
    DocumentIntake,
    IntakeState,
    ScanVerdict,
)
from apps.cycles.export_records import EvidenceExport
from apps.cycles.models import EvidenceLink, ServiceCycle
from apps.cycles.models import Exception as CycleException
from apps.cycles.retention import physical_identity, place_hold, release_hold
from apps.cycles.retention_records import ContentPurgeClaim
from apps.cycles.retention_tasks import Candidate, disposal_refusal, purge_tenant
from apps.cycles.scanning import FakeScanner
from apps.cycles.services.federal_review import (
    record_federal_report_review,
    report_input_digest,
)
from apps.cycles.services.requests import RequestFields, create_request
from apps.integrations.call_models import ProviderCall
from apps.integrations.serpro import retention as serpro_retention
from apps.integrations.serpro.pgmei import OPERATION as GUIDE_OPERATION
from apps.integrations.serpro.pgmei import REQUESTED_EVENT
from apps.obligations.models import (
    AcquisitionOrigin,
    Document,
    FiscalSituationReport,
    Obligation,
    ObligationType,
)
from apps.obligations.system_ingest import ingest_system_document
from tests.integrations.sitfis_support import (
    PDF,
    World,
    clock,
    protocol_reply,
    report_reply,
    world,
)
from tests.ui.factories import make_firm

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "world"]

SAO_PAULO = ZoneInfo("America/Sao_Paulo")


class RecordingBucket(S3Storage):
    """An S3 backend double that records every storage call and sends nothing."""

    def __init__(self) -> None:
        super().__init__(
            bucket_name="axrm051-synthetic",
            endpoint_url="https://storage.invalid",
            access_key="synthetic-access",
            secret_key="synthetic-secret",  # noqa: S106 - no request is ever sent
        )
        self.calls: list[tuple[str, str]] = []

    def exists(self, name: str) -> bool:
        self.calls.append(("exists", name))
        return True

    def delete(self, name: str) -> None:
        self.calls.append(("delete", name))

    def _open(self, name: str, mode: str = "rb") -> ContentFile[bytes]:
        self.calls.append(("open", name))
        return ContentFile(PDF)


class VersionedFiles(FileSystemStorage):
    """A backend the allow-list does not name (it could keep versions)."""


def _use_storage(monkeypatch: pytest.MonkeyPatch, storage: Storage) -> None:
    monkeypatch.setattr(retention, "default_storage", storage)
    monkeypatch.setattr(retention_tasks, "default_storage", storage)


def _emit(world: World) -> FiscalSituationReport:
    world.script(protocol_reply(30_000))
    report = world.advance(world.request())
    world.clock.advance(seconds=30)
    world.script(report_reply())
    emitted = world.advance(report)
    assert emitted.state == "emitido"
    return world.reload(emitted)


def _original(world: World, report: FiscalSituationReport) -> Document:
    assert report.document_id is not None
    with tenant_context(world.tenant_id):
        return Document.objects.get(pk=report.document_id)


def _expiry(report: FiscalSituationReport) -> datetime:
    assert report.emitted_at is not None
    year = timezone.localdate(report.emitted_at).year + 6
    return datetime(year, 1, 1, tzinfo=SAO_PAULO)


def _claim(world: World, document_id: UUID | None) -> ContentPurgeClaim | None:
    assert document_id is not None
    with tenant_context(world.tenant_id):
        return ContentPurgeClaim.objects.filter(content_id=document_id).first()


def _staff_original(
    world: World, payload: bytes, competence: date | None = None
) -> Document:
    """A private, clean staff upload, as a firm stores one before SERPRO reuses it."""
    termo = world.termo
    with tenant_context(world.tenant_id):
        obligation = None
        if competence is not None:
            kind, _ = ObligationType.objects.get_or_create(
                code="DAS", defaults={"name": "DAS", "periodicity": "monthly"}
            )
            obligation = Obligation.objects.create(
                tenant=termo.tenant,
                client=termo.client,
                obligation_type=kind,
                competence_month=competence,
                nominal_due_date=competence,
                resolved_due_date=competence,
            )
        document = Document.objects.create(
            tenant=termo.tenant,
            client=termo.client,
            obligation=obligation,
            storage_key=default_storage.save(
                f"originals/{uuid4().hex}.pdf", ContentFile(payload)
            ),
            sha256=hashlib.sha256(payload).hexdigest(),
            original_filename="staff-051.pdf",
            content_type="application/pdf",
            byte_size=len(payload),
            uploaded_by=termo.actor,
            acquisition_origin=AcquisitionOrigin.UPLOAD_STAFF,
            portal_visible=False,
        )
        DocumentIntake.objects.create(
            tenant=termo.tenant,
            client=termo.client,
            document=document,
            state=IntakeState.VERIFIED,
            verdict=ScanVerdict.CLEAN,
            scanned_at=timezone.now(),
        )
        return document


def _review(world: World, report: FiscalSituationReport) -> None:
    with tenant_context(world.tenant_id):
        cycle = ServiceCycle.objects.get(pk=world.cycle.pk)
        current = FiscalSituationReport.objects.select_related("document").get(
            pk=report.pk
        )
        record_federal_report_review(
            world.responsible,
            cycle.pk,
            report.pk,
            outcome="sem_pendencias_nas_fontes",
            pendencias=[],
            expected_version=cycle.version,
            input_digest=report_input_digest(current),
        )


def test_a_persisted_staff_original_a_report_holds_is_not_disposed_on_s3(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    staff = _staff_original(world, PDF)
    report = _emit(world)
    world.scan()
    # The emission reused the staff upload: the report holds a staff-origin row.
    assert report.document_id == staff.pk
    bucket = RecordingBucket()
    _use_storage(monkeypatch, bucket)
    counts = purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))
    assert (counts["eligible"], counts["purged"]) == (0, 0)
    assert [call for call in bucket.calls if call[0] == "delete"] == []
    assert _claim(world, staff.pk) is None


def test_an_unrecognized_backend_is_unsupported_for_federal_disposal(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _emit(world)
    world.scan()
    _use_storage(monkeypatch, VersionedFiles(location=settings.MEDIA_ROOT))
    candidate = Candidate(
        world.termo.client.pk, "document", _original(world, report).pk, None
    )
    with tenant_context(world.tenant_id):
        assert disposal_refusal(candidate) == "armazenamento_nao_suportado"
        # An evidence archive of this client is federal content too.
        archive = Candidate(world.termo.client.pk, "export", uuid4(), None)
        assert disposal_refusal(archive) == "armazenamento_nao_suportado"
    purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))
    assert _claim(world, report.document_id) is None
    assert default_storage.exists(_original(world, report).storage_key)


def test_a_final_file_missing_before_the_claim_is_never_completed(world: World) -> None:
    report = _emit(world)
    world.scan()
    original = _original(world, report)
    default_storage.delete(original.storage_key)
    counts = purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))
    assert counts["purged"] == 0
    claim = _claim(world, original.pk)
    assert claim is not None
    assert (claim.completed_at, claim.error_code) == (None, "arquivo_ausente")
    with tenant_context(world.tenant_id):
        assert Document.objects.get(pk=original.pk).purged_at is None


def test_bytes_that_are_not_the_original_are_kept(world: World) -> None:
    report = _emit(world)
    world.scan()
    original = _original(world, report)
    default_storage.delete(original.storage_key)
    default_storage.save(original.storage_key, ContentFile(b"another document"))
    purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))
    claim = _claim(world, original.pk)
    assert claim is not None
    assert (claim.completed_at, claim.error_code) == (None, "conteudo_divergente")
    assert default_storage.exists(original.storage_key)


def test_a_retry_after_a_successful_delete_keeps_a_replacement(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _emit(world)
    world.scan()
    original = _original(world, report)
    delete = default_storage.delete

    def delete_then_die(key: str) -> None:
        delete(key)
        if key == original.storage_key:  # the final is gone; the worker dies
            message = "worker lost after the delete"
            raise OSError(message)

    monkeypatch.setattr(default_storage, "delete", delete_then_die)
    assert (
        purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))["failed"] == 1
    )
    monkeypatch.setattr(default_storage, "delete", delete)
    assert not default_storage.exists(original.storage_key)
    # Something new is written at the same key before the retry.
    assert (
        default_storage.save(original.storage_key, ContentFile(b"replacement"))
        == original.storage_key
    )
    counts = purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))
    assert counts["purged"] == 0
    with default_storage.open(original.storage_key, "rb") as handle:
        assert handle.read() == b"replacement"
    claim = _claim(world, original.pk)
    assert claim is not None
    assert (claim.completed_at, claim.error_code) == (None, "conteudo_divergente")


def test_a_held_clients_quarantine_copy_is_kept_by_the_scanner(world: World) -> None:
    report = _emit(world)
    with tenant_context(world.tenant_id):
        client = world.termo.client.__class__.objects.get(pk=world.termo.client.pk)
        place_hold(
            world.termo.actor,
            client.pk,
            reason="Fiscalização em andamento",
            expected_version=client.version,
        )
        intake = DocumentIntake.objects.get(document=_original(world, report))
    world.scan()
    with tenant_context(world.tenant_id):
        assert DocumentIntake.objects.get(pk=intake.pk).state in CLEAN_STATES
    assert default_storage.exists(intake.object_key)
    assert default_storage.exists(_original(world, report).storage_key)


def test_a_stale_scanner_never_writes_a_disposed_original_back(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _emit(world)
    with tenant_context(world.tenant_id):
        intake = DocumentIntake.objects.get(document=_original(world, report))
    # Scanner A takes its ticket; scanner B finishes first and the purge follows.
    stale = scanning.claim_scan(world.tenant_id, intake.pk)
    assert stale is not None
    world.scan()
    assert (
        purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))["purged"] == 1
    )
    original = _original(world, report)
    assert not default_storage.exists(original.storage_key)
    default_storage.save(stale.object_key, ContentFile(PDF))
    saved: list[str] = []
    save = default_storage.save

    def recorded(name: str, content: ContentFile[bytes]) -> str:
        saved.append(name)
        return save(name, content)

    monkeypatch.setattr(default_storage, "save", recorded)
    scanning.promote(stale, default_storage)
    assert not default_storage.exists(original.storage_key)
    assert saved == []  # the stale ticket writes nothing at all


def test_a_stale_scanner_never_removes_what_another_scanner_verified(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _emit(world)
    original = _original(world, report)
    with tenant_context(world.tenant_id):
        intake = DocumentIntake.objects.get(document=original)
    first = scanning.claim_scan(world.tenant_id, intake.pk)
    assert first is not None
    create = scanning._create_exclusive
    interleaved: list[str] = []

    def create_then_let_the_other_scanner_finish(
        storage: FileSystemStorage, key: str, payload: bytes
    ) -> None:
        # Scanner A's write of the final (its exclusive create), then scanner B.
        create(storage, key, payload)
        if not interleaved:
            interleaved.append(key)
            monkeypatch.setattr(scanning, "_create_exclusive", create)
            world.scan()  # scanner B verifies A's copy and removes the quarantine

    monkeypatch.setattr(
        scanning, "_create_exclusive", create_then_let_the_other_scanner_finish
    )
    scanning.promote(first, default_storage)
    assert interleaved == [original.storage_key]
    with tenant_context(world.tenant_id):
        assert DocumentIntake.objects.get(pk=intake.pk).state in CLEAN_STATES
    assert default_storage.exists(original.storage_key)


def test_a_review_is_refused_once_the_original_was_disposed_of(world: World) -> None:
    report = _emit(world)
    world.scan()
    assert (
        purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))["purged"] == 1
    )
    with pytest.raises(ValidationError):
        _review(world, report)


def test_unresolvable_references_are_kept_and_the_sweep_continues(
    world: World,
) -> None:
    report = _emit(world)
    world.scan()
    with tenant_context(world.tenant_id):
        sitfis_call = ProviderCall.objects.get(operation="SP-SIT-R")
    orphan = ingest_system_document(
        tenant_id=world.tenant_id,
        client_id=world.termo.client.pk,
        origin=AcquisitionOrigin.FEDERAL_PROVIDER,
        receipt_ref=str(sitfis_call.pk),
        payload=b"%PDF-1.7\n% a SITFIS call no report holds AXRM-051\n%%EOF\n",
        filename="sem-relatorio.pdf",
        content_type="application/pdf",
        retention_class="fiscal_evidence",
    )
    far_future = _staff_original(
        world, b"%PDF-1.7\n% year 9999\n%%EOF\n", date(9999, 6, 1)
    )
    with tenant_context(world.tenant_id):
        assert retention_tasks._document_anchors(orphan)[1] == "relatorio_ausente"
        assert retention_tasks._document_retention(far_future)[2] == (
            "data_fora_do_intervalo"
        )
        Event.objects.create(
            tenant_id=world.tenant_id,
            actor=world.termo.actor,
            action=REQUESTED_EVENT,
            object_type="ServiceCycle",
            object_id="legacy-cycle",
            metadata={"request_key": "legacy-guide-request"},
        )
        restored = ProviderCall(
            client_id=world.termo.client.pk,
            operation=GUIDE_OPERATION,
            request_key="legacy-guide-request",
        )
        assert serpro_retention._guide_anchors(restored) == ([], ["pedido_invalido"])
    purge_tenant(
        world.tenant_id, dry_run=False, now=datetime(2040, 1, 1, tzinfo=SAO_PAULO)
    )
    assert _claim(world, orphan.pk) is None
    assert _claim(world, far_future.pk) is None
    # The valid neighbour in the same sweep was disposed of.
    claim = _claim(world, report.document_id)
    assert claim is not None
    assert claim.completed_at is not None


def test_a_reference_into_another_tenant_never_dates_an_original(world: World) -> None:
    report = _emit(world)
    world.scan()
    with tenant_context(world.tenant_id):
        local_call = ProviderCall.objects.get(operation="SP-SIT-R").pk
        local_key = _original(world, report).storage_key
    foreign = make_firm("axrm051-foreign", client_count=1)
    foreign.tenant.features = ["cycles_r1", "scan_required", "federal_checks"]
    foreign.tenant.save(update_fields=["features"])
    borrowed = ingest_system_document(
        tenant_id=foreign.tenant.pk,
        client_id=foreign.clients[0].pk,
        origin=AcquisitionOrigin.FEDERAL_PROVIDER,
        receipt_ref=str(local_call),
        payload=b"%PDF-1.7\n% names another tenant's call AXRM-051\n%%EOF\n",
        filename="alheio.pdf",
        content_type="application/pdf",
        retention_class="fiscal_evidence",
    )
    with tenant_context(foreign.tenant.pk):
        assert retention_tasks._document_anchors(borrowed)[1] == "chamada_ausente"
    later = datetime(2040, 1, 1, tzinfo=SAO_PAULO)
    assert purge_tenant(foreign.tenant.pk, dry_run=False, now=later)["purged"] == 0
    with tenant_context(foreign.tenant.pk):
        assert not ContentPurgeClaim.objects.exists()
    # The same reference inside its own tenant dates the local original.
    with tenant_context(world.tenant_id):
        assert retention_tasks._document_anchors(_original(world, report))[1] == ""
        assert not ContentPurgeClaim.objects.exists()
    assert default_storage.exists(local_key)


def test_a_key_another_clients_row_names_is_not_disposed_of(world: World) -> None:
    report = _emit(world)
    world.scan()
    original = _original(world, report)
    with tenant_context(world.tenant_id):
        sibling = world.termo.client.__class__.objects.create(
            tenant=world.termo.tenant, cnpj="11444777000161", legal_name="Sibling"
        )
        # A restored, inconsistent row of another client naming the same object.
        Document.objects.create(
            tenant=world.termo.tenant,
            client=sibling,
            storage_key=original.storage_key,
            sha256=original.sha256,
            original_filename="alias.pdf",
            content_type="application/pdf",
            byte_size=original.byte_size,
            uploaded_by=world.termo.actor,
            acquisition_origin=AcquisitionOrigin.UPLOAD_STAFF,
            portal_visible=False,
        )
        candidate = next(
            c for c in retention_tasks._candidates() if c.id == original.pk
        )
        assert disposal_refusal(candidate) == "chave_compartilhada"
    purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))
    assert _claim(world, original.pk) is None
    assert default_storage.exists(original.storage_key)


@pytest.mark.parametrize("leftover", ["same_bytes", "other_bytes"])
def test_a_kept_copy_is_disposed_of_only_when_it_is_the_original(
    world: World, leftover: str
) -> None:
    report = _emit(world)
    with tenant_context(world.tenant_id):
        client = world.termo.client.__class__.objects.get(pk=world.termo.client.pk)
        hold = place_hold(
            world.termo.actor,
            client.pk,
            reason="Fiscalização em andamento",
            expected_version=client.version,
        )
        intake = DocumentIntake.objects.get(document=_original(world, report))
    world.scan()  # the held client's quarantine copy is kept
    assert default_storage.exists(intake.object_key)
    if leftover == "other_bytes":
        default_storage.delete(intake.object_key)
        default_storage.save(intake.object_key, ContentFile(b"not the original"))
    with tenant_context(world.tenant_id):
        release_hold(world.termo.actor, hold.pk, expected_version=hold.version)
    purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))
    claim = _claim(world, report.document_id)
    assert claim is not None
    if leftover == "same_bytes":
        # Both copies are proved the original's and both are gone.
        assert claim.completed_at is not None
        assert not default_storage.exists(intake.object_key)
        assert not default_storage.exists(_original(world, report).storage_key)
    else:
        assert (claim.completed_at, claim.error_code) == (None, "conteudo_divergente")
        assert default_storage.exists(intake.object_key)


def test_a_delete_that_leaves_the_bytes_never_completes_a_claim(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _emit(world)
    world.scan()
    monkeypatch.setattr(default_storage, "delete", lambda key: None)
    purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))
    claim = _claim(world, report.document_id)
    assert claim is not None
    assert (claim.completed_at, claim.error_code) == (None, "exclusao_nao_comprovada")


def test_a_federal_original_without_an_intake_is_not_disposed_of(
    world: World,
) -> None:
    termo = world.termo
    with tenant_context(world.tenant_id):
        original = Document.objects.create(
            tenant=termo.tenant,
            client=termo.client,
            storage_key=default_storage.save(
                f"originals/{uuid4().hex}.pdf", ContentFile(PDF)
            ),
            sha256=hashlib.sha256(PDF).hexdigest(),
            original_filename="sem-intake.pdf",
            acquisition_receipt_ref=str(uuid4()),
            content_type="application/pdf",
            byte_size=len(PDF),
            uploaded_by=termo.actor,
            acquisition_origin=AcquisitionOrigin.FEDERAL_PROVIDER,
            portal_visible=False,
        )
        candidate = Candidate(termo.client.pk, "document", original.pk, None)
        assert disposal_refusal(candidate) == "verificacao_pendente"


def _clone_call(world: World, operation: str, state: str) -> ProviderCall:
    """Another call of the client like an existing one, with no record of its own."""
    with tenant_context(world.tenant_id):
        model = ProviderCall.objects.filter(operation=operation).first()
        assert model is not None
        columns = {
            field.attname: getattr(model, field.attname)
            for field in ProviderCall._meta.concrete_fields
            if not field.primary_key
        }
        columns.update(state=state, request_key=f"{model.request_key[:40]}-clone")
        return ProviderCall.objects.create(**columns)


def test_a_report_call_without_its_attachment_keeps_private_originals(
    world: World,
) -> None:
    report = _emit(world)
    world.scan()
    staff = _staff_original(
        world, b"%PDF-1.7\n% staff original 2018\n%%EOF\n", date(2018, 9, 1)
    )
    # A later SP-SIT-R call that may have reused the bytes, with no report holding it.
    later = _clone_call(world, "SP-SIT-R", "sucesso")
    with tenant_context(world.tenant_id):
        assert retention_tasks._document_anchors(staff)[1] == "proveniencia_incompleta"
        # A report holding an original resolves only its own call, never another.
        original = _original(world, report)
        assert (
            retention_tasks._document_anchors(original)[1] == "proveniencia_incompleta"
        )
        original.acquisition_receipt_ref = str(later.pk)
        assert serpro_retention._acquisition_anchors(original) == (
            [],
            ["relatorio_ausente"],
        )
    purge_tenant(
        world.tenant_id, dry_run=False, now=datetime(2040, 1, 1, tzinfo=SAO_PAULO)
    )
    assert _claim(world, staff.pk) is None
    assert default_storage.exists(staff.storage_key)


def test_an_ordinary_row_never_deletes_a_held_federal_siblings_key(
    world: World,
) -> None:
    report = _emit(world)
    world.scan()
    original = _original(world, report)
    termo = world.termo
    with tenant_context(world.tenant_id):
        client = termo.client.__class__.objects.get(pk=termo.client.pk)
        place_hold(
            termo.actor,
            client.pk,
            reason="Fiscalização",
            expected_version=client.version,
        )
        sibling = termo.client.__class__.objects.create(
            tenant=termo.tenant, cnpj="11444777000161", legal_name="Sibling"
        )
        kind, _ = ObligationType.objects.get_or_create(
            code="DAS", defaults={"name": "DAS", "periodicity": "monthly"}
        )
        old = Obligation.objects.create(
            tenant=termo.tenant,
            client=sibling,
            obligation_type=kind,
            competence_month=date(2018, 9, 1),
            nominal_due_date=date(2018, 10, 20),
            resolved_due_date=date(2018, 10, 20),
        )
        alias = Document.objects.create(
            tenant=termo.tenant,
            client=sibling,
            obligation=old,
            storage_key=original.storage_key,
            sha256=original.sha256,
            original_filename="alias.pdf",
            content_type="application/pdf",
            byte_size=original.byte_size,
            uploaded_by=termo.actor,
            acquisition_origin=AcquisitionOrigin.UPLOAD_STAFF,
            portal_visible=False,
        )
    purge_tenant(
        world.tenant_id, dry_run=False, now=datetime(2040, 1, 1, tzinfo=SAO_PAULO)
    )
    with tenant_context(world.tenant_id):
        assert not ContentPurgeClaim.objects.filter(content_id=alias.pk).exists()
    assert default_storage.exists(original.storage_key)


def test_a_key_another_session_holds_is_not_deleted(world: World) -> None:
    report = _emit(world)
    world.scan()
    original = _original(world, report)
    other = connection.get_new_connection(connection.get_connection_params())
    try:
        other.autocommit = True
        lock_id = retention._key_lock_id(original.storage_key)
        other.execute("SELECT pg_advisory_lock(%s)", [lock_id])
        counts = purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))
        assert counts["purged"] == 0
        claim = _claim(world, original.pk)
        assert claim is not None
        assert (claim.completed_at, claim.error_code) == (None, "chave_em_uso")
        assert default_storage.exists(original.storage_key)
        other.execute("SELECT pg_advisory_unlock(%s)", [lock_id])
    finally:
        other.close()
    # Released, the next run proves and completes the same claim once.
    assert (
        purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))["purged"] == 1
    )
    claim = _claim(world, original.pk)
    assert claim is not None
    assert claim.completed_at is not None
    keys = tuple(claim.storage_keys)
    # A worker still carrying the completed claim re-reads it under the key lock
    # and leaves alone what has since been written at the key.
    default_storage.save(original.storage_key, ContentFile(b"written later"))
    assert (
        retention_tasks._dispose(
            world.tenant_id,
            client_id=claim.client_id,
            claim_id=claim.pk,
            keys=keys,
            identity=None,
            now=_expiry(report),
        )
        == "concluida"
    )
    assert default_storage.exists(original.storage_key)


def test_the_scanner_removes_a_quarantine_copy_only_under_the_client_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _emit(world)
    observed: list[tuple[bool, list[str]]] = []
    discard = scanning._discard

    def watched(storage: Storage, key: str) -> None:
        if key.startswith("quarantine/"):
            locks = [
                query["sql"]
                for query in connection.queries
                if "clients_clientcompany" in query["sql"]
                and "FOR NO KEY UPDATE" in query["sql"]
            ]
            observed.append((connection.in_atomic_block, locks))
        discard(storage, key)

    monkeypatch.setattr(scanning, "_discard", watched)
    with CaptureQueriesContext(connection):
        world.scan()
    assert observed, "the verified quarantine copy was removed"
    in_transaction, locks = observed[0]
    assert in_transaction
    assert locks


def test_a_scanner_never_writes_a_key_a_disposal_holds(world: World) -> None:
    report = _emit(world)
    original = _original(world, report)
    with tenant_context(world.tenant_id):
        intake = DocumentIntake.objects.get(document=original)
    ticket = scanning.claim_scan(world.tenant_id, intake.pk)
    assert ticket is not None
    other = connection.get_new_connection(connection.get_connection_params())
    try:
        other.autocommit = True
        lock_id = retention._key_lock_id(original.storage_key)
        other.execute("SELECT pg_advisory_lock(%s)", [lock_id])
        with pytest.raises(scanning.ScanDeferred):
            scanning.promote(ticket, default_storage)
        assert not default_storage.exists(original.storage_key)
        other.execute("SELECT pg_advisory_unlock(%s)", [lock_id])
    finally:
        other.close()
    scanning.promote(ticket, default_storage)
    assert default_storage.exists(original.storage_key)


def test_a_provider_row_naming_a_report_call_never_discharges_another_original(
    world: World,
) -> None:
    _emit(world)
    world.scan()
    staff = _staff_original(
        world, b"%PDF-1.7\n% staff original reused by C\n%%EOF\n", date(2018, 9, 1)
    )
    later = _clone_call(world, "SP-SIT-R", "sucesso")
    # A restored provider row names the call C, but no report attaches it.
    ingest_system_document(
        tenant_id=world.tenant_id,
        client_id=world.termo.client.pk,
        origin=AcquisitionOrigin.FEDERAL_PROVIDER,
        receipt_ref=str(later.pk),
        payload=b"%PDF-1.7\n% restored row naming C\n%%EOF\n",
        filename="restaurado.pdf",
        content_type="application/pdf",
        retention_class="fiscal_evidence",
    )
    with tenant_context(world.tenant_id):
        assert retention_tasks._document_anchors(staff)[1] == "proveniencia_incompleta"


def test_an_unrepresentable_archive_date_keeps_the_original_and_the_sweep_goes_on(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _emit(world)
    world.scan()
    original = _original(world, report)
    pending = SimpleNamespace(
        expires_at=None, created_at=datetime(9999, 12, 20, tzinfo=UTC)
    )
    real_filter = EvidenceExport.objects.filter

    def with_pending(*args: object, **kwargs: object) -> object:
        if "index__data__documents__contains" in kwargs:
            return [pending]
        return real_filter(*args, **kwargs)

    monkeypatch.setattr(EvidenceExport.objects, "filter", with_pending)
    with tenant_context(world.tenant_id):
        try:
            outcome: tuple[object, ...] | None = retention_tasks._document_retention(
                original
            )
        except OverflowError:
            outcome = None
    assert outcome is not None
    assert (outcome[0], outcome[2]) == (None, "data_fora_do_intervalo")
    try:
        counts: dict[str, int] | None = purge_tenant(
            world.tenant_id, dry_run=False, now=_expiry(report)
        )
    except OverflowError:
        counts = None
    assert counts is not None
    assert _claim(world, original.pk) is None


def test_the_scanner_keeps_a_quarantine_key_another_clients_row_names(
    world: World,
) -> None:
    report = _emit(world)
    original = _original(world, report)
    termo = world.termo
    with tenant_context(world.tenant_id):
        intake = DocumentIntake.objects.get(document=original)
        sibling = termo.client.__class__.objects.create(
            tenant=termo.tenant, cnpj="11444777000161", legal_name="Sibling"
        )
        # A held sibling's restored original names this intake's quarantine key.
        Document.objects.create(
            tenant=termo.tenant,
            client=sibling,
            storage_key=intake.quarantine_key,
            sha256=original.sha256,
            original_filename="alias.pdf",
            content_type="application/pdf",
            byte_size=original.byte_size,
            uploaded_by=termo.actor,
            acquisition_origin=AcquisitionOrigin.UPLOAD_STAFF,
            portal_visible=False,
        )
        place_hold(
            termo.actor,
            sibling.pk,
            reason="Fiscalização",
            expected_version=sibling.version,
        )
    world.scan()
    with tenant_context(world.tenant_id):
        assert DocumentIntake.objects.get(pk=intake.pk).state in CLEAN_STATES
    assert default_storage.exists(intake.quarantine_key)


def test_a_scanner_that_loses_the_key_lock_defers_instead_of_failing(
    world: World,
) -> None:
    report = _emit(world)
    original = _original(world, report)
    with tenant_context(world.tenant_id):
        intake = DocumentIntake.objects.get(document=original)
    late = intake.created_at + timedelta(hours=25)
    other = connection.get_new_connection(connection.get_connection_params())
    try:
        other.autocommit = True
        lock_id = retention._key_lock_id(original.storage_key)
        other.execute("SELECT pg_advisory_lock(%s)", [lock_id])
        outcome = scanning.scan_intake(
            world.tenant_id, intake.pk, scanner=FakeScanner(), now=late
        )
        other.execute("SELECT pg_advisory_unlock(%s)", [lock_id])
    finally:
        other.close()
    assert outcome == scanning.ScanOutcome.SKIPPED
    with tenant_context(world.tenant_id):
        assert DocumentIntake.objects.get(pk=intake.pk).state in PENDING_SCAN_STATES
    # The winner's later scan is still a clean verdict.
    assert (
        scanning.scan_intake(
            world.tenant_id, intake.pk, scanner=FakeScanner(), now=late
        )
        == scanning.ScanOutcome.VERIFIED
    )


def test_a_scan_never_verifies_an_original_whose_disposal_started(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    termo = world.termo
    payload = b"%PDF-1.7\n% ordinary original in quarantine\n%%EOF\n"
    with tenant_context(world.tenant_id):
        kind, _ = ObligationType.objects.get_or_create(
            code="DAS", defaults={"name": "DAS", "periodicity": "monthly"}
        )
        old = Obligation.objects.create(
            tenant=termo.tenant,
            client=termo.client,
            obligation_type=kind,
            competence_month=date(2018, 9, 1),
            nominal_due_date=date(2018, 10, 20),
            resolved_due_date=date(2018, 10, 20),
        )
        quarantined = default_storage.save(
            f"quarantine/{termo.tenant.pk}/{termo.client.pk}/{uuid4().hex}",
            ContentFile(payload),
        )
        document = Document.objects.create(
            tenant=termo.tenant,
            client=termo.client,
            obligation=old,
            storage_key=f"originals/{uuid4().hex}.pdf",
            sha256=hashlib.sha256(payload).hexdigest(),
            original_filename="pendente.pdf",
            content_type="application/pdf",
            byte_size=len(payload),
            uploaded_by=termo.actor,
            acquisition_origin=AcquisitionOrigin.UPLOAD_STAFF,
            portal_visible=False,
        )
        intake = DocumentIntake.objects.create(
            tenant=termo.tenant,
            client=termo.client,
            document=document,
            state=IntakeState.QUARANTINED,
            # The model default reads the wall clock; ages follow the injected one.
            created_at=timezone.now(),
            quarantine_key=quarantined,
        )

    def refuse(key: str) -> None:
        message = "synthetic storage refusal"
        raise OSError(message)

    monkeypatch.setattr(default_storage, "delete", refuse)
    purge_tenant(
        world.tenant_id, dry_run=False, now=datetime(2030, 1, 1, tzinfo=SAO_PAULO)
    )
    monkeypatch.undo()
    assert _claim(world, document.pk) is not None
    outcome = scanning.scan_intake(
        world.tenant_id, intake.pk, scanner=FakeScanner(), now=timezone.now()
    )
    assert outcome == scanning.ScanOutcome.SKIPPED
    with tenant_context(world.tenant_id):
        assert DocumentIntake.objects.get(pk=intake.pk).state == IntakeState.QUARANTINED
    assert not default_storage.exists(document.storage_key)
    assert default_storage.exists(quarantined)


def test_private_reuse_refuses_to_run_without_the_callers_transaction(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"%PDF-1.7\n% reused outside a transaction\n%%EOF\n"
    _staff_original(world, payload)
    outside = SimpleNamespace(in_atomic_block=False)
    monkeypatch.setattr("apps.cycles.intake.connection", outside)
    with tenant_context(world.tenant_id), pytest.raises(TransactionManagementError):
        intake_module.staff_upload(
            world.termo.actor,
            SimpleUploadedFile("reuso.pdf", payload, content_type="application/pdf"),
            tenant=world.termo.tenant,
            client=world.termo.client,
        )


def test_an_unrepresentable_derived_date_means_no_expiry() -> None:
    try:
        derived: datetime | str | None = retention_tasks._after(
            datetime(9999, 6, 1, tzinfo=UTC), 5
        )
    except (OverflowError, ValueError) as error:
        derived = f"raised {type(error).__name__}"
    assert derived is None
    assert retention_tasks._after(datetime(2020, 2, 29, tzinfo=UTC), 1) == datetime(
        2021, 2, 28, tzinfo=UTC
    )


def test_the_scanner_never_deletes_a_quarantine_copy_on_remote_storage(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _emit(world)
    with tenant_context(world.tenant_id):
        intake = DocumentIntake.objects.get(document=_original(world, report))
    ticket = scanning.claim_scan(world.tenant_id, intake.pk)
    assert ticket is not None
    bucket = RecordingBucket()
    # As in production, the remote store also holds the rows' objects.
    monkeypatch.setattr(retention, "rows_storage", lambda: bucket, raising=False)
    scanning._discard_quarantine(ticket, bucket)
    assert bucket.calls == []


def _ordinary_pending(
    world: World, *, with_final: bool
) -> tuple[Document, DocumentIntake]:
    """An expired ordinary original (2018) with a pending intake and its copy."""
    termo = world.termo
    # Distinct bytes per call: a world may hold several such originals.
    payload = b"%%PDF-1.7\n%% ordinary original AXRM-051 r5 %s\n%%%%EOF\n" % (
        uuid4().hex.encode()
    )
    with tenant_context(world.tenant_id):
        kind, _ = ObligationType.objects.get_or_create(
            code="DAS", defaults={"name": "DAS", "periodicity": "monthly"}
        )
        # One expired obligation per world; a second original joins it.
        old, _ = Obligation.objects.get_or_create(
            tenant=termo.tenant,
            client=termo.client,
            obligation_type=kind,
            competence_month=date(2018, 9, 1),
            defaults={
                "nominal_due_date": date(2018, 10, 20),
                "resolved_due_date": date(2018, 10, 20),
            },
        )
        key = f"originals/{uuid4().hex}.pdf"
        if with_final:
            key = default_storage.save(key, ContentFile(payload))
        document = Document.objects.create(
            tenant=termo.tenant,
            client=termo.client,
            obligation=old,
            storage_key=key,
            sha256=hashlib.sha256(payload).hexdigest(),
            original_filename="pendente.pdf",
            content_type="application/pdf",
            byte_size=len(payload),
            uploaded_by=termo.actor,
            acquisition_origin=AcquisitionOrigin.UPLOAD_STAFF,
            portal_visible=False,
        )
        intake = DocumentIntake.objects.create(
            tenant=termo.tenant,
            client=termo.client,
            document=document,
            state=IntakeState.QUARANTINED,
            # The model default reads the wall clock; ages follow the injected one.
            created_at=timezone.now(),
            quarantine_key=default_storage.save(
                f"quarantine/{termo.tenant.pk}/{termo.client.pk}/{uuid4().hex}",
                ContentFile(payload),
            ),
        )
    return document, intake


def test_a_retry_with_a_valid_final_never_verifies_a_claimed_original(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    document, intake = _ordinary_pending(world, with_final=True)

    def refuse(key: str) -> None:
        message = "synthetic storage refusal"
        raise OSError(message)

    monkeypatch.setattr(default_storage, "delete", refuse)
    purge_tenant(
        world.tenant_id, dry_run=False, now=datetime(2030, 1, 1, tzinfo=SAO_PAULO)
    )
    monkeypatch.undo()
    assert _claim(world, document.pk) is not None
    outcome = scanning.scan_intake(
        world.tenant_id, intake.pk, scanner=FakeScanner(), now=timezone.now()
    )
    assert outcome == scanning.ScanOutcome.SKIPPED
    with tenant_context(world.tenant_id):
        assert DocumentIntake.objects.get(pk=intake.pk).state == IntakeState.QUARANTINED


def test_cleanup_keeps_a_quarantine_file_that_is_not_the_promoted_original(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _emit(world)
    with tenant_context(world.tenant_id):
        intake = DocumentIntake.objects.get(document=_original(world, report))
    record = scanning.record_verdict

    def then_replaced(*args: object, **kwargs: object) -> scanning.ScanOutcome:
        outcome = record(*args, **kwargs)  # type: ignore[arg-type]
        default_storage.delete(intake.quarantine_key)
        default_storage.save(intake.quarantine_key, ContentFile(b"another upload"))
        return outcome

    monkeypatch.setattr(scanning, "record_verdict", then_replaced)
    world.scan()
    assert default_storage.exists(intake.quarantine_key)
    with default_storage.open(intake.quarantine_key, "rb") as handle:
        assert handle.read() == b"another upload"


def test_a_key_another_tenants_row_names_is_never_deleted(world: World) -> None:
    report = _emit(world)
    world.scan()
    original = _original(world, report)
    foreign = make_firm("axrm051-r5-foreign", client_count=1)
    with tenant_context(foreign.tenant.pk):
        Document.objects.create(
            tenant=foreign.tenant,
            client=foreign.clients[0],
            storage_key=original.storage_key,
            sha256=original.sha256,
            original_filename="alheio.pdf",
            content_type="application/pdf",
            byte_size=original.byte_size,
            uploaded_by=foreign.owner,
            acquisition_origin=AcquisitionOrigin.UPLOAD_STAFF,
            portal_visible=False,
        )
    purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))
    assert _claim(world, original.pk) is None
    assert default_storage.exists(original.storage_key)


@pytest.mark.parametrize(
    "emitted",
    [datetime(1, 1, 1, 0, 30, tzinfo=UTC), datetime(9999, 12, 31, 23, 59, tzinfo=UTC)],
)
def test_an_emission_at_either_calendar_edge_keeps_the_original(
    world: World, emitted: datetime
) -> None:
    report = _emit(world)
    world.scan()
    current = world.reload(report)
    current.emitted_at = emitted
    with tenant_context(world.tenant_id):
        try:
            anchors, gaps = serpro_retention._report_anchors(current)
            outcome = "anchored"
        except (OverflowError, ValueError):
            anchors, gaps, outcome = [], [], "raised"
    if emitted.year == 1:
        # No local date exists for it: kept with a reason, never an abort.
        assert (outcome, anchors, gaps) == ("anchored", [], ["data_fora_do_intervalo"])
    else:
        # A local date exists; its year + 6 does not, which the expiry keeps.
        assert (outcome, gaps) == ("anchored", [])
        with pytest.raises(ValueError, match="year"):
            retention.fiscal_expiry(anchors)


def test_a_recovered_claim_never_deletes_keys_the_row_no_longer_agrees_on(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    document, intake = _ordinary_pending(world, with_final=True)

    def refuse(key: str) -> None:
        message = "synthetic storage refusal"
        raise OSError(message)

    monkeypatch.setattr(default_storage, "delete", refuse)
    when = datetime(2030, 1, 1, tzinfo=SAO_PAULO)
    purge_tenant(world.tenant_id, dry_run=False, now=when)
    monkeypatch.undo()
    claim = _claim(world, document.pk)
    assert claim is not None
    # The row now names another copy than the one the claim was taken for.
    moved = default_storage.save(
        f"quarantine/{world.tenant_id}/{document.client_id}/{uuid4().hex}",
        ContentFile(b"another copy"),
    )
    with tenant_context(world.tenant_id):
        DocumentIntake.objects.filter(pk=intake.pk).update(quarantine_key=moved)
    purge_tenant(world.tenant_id, dry_run=False, now=when)
    claim = _claim(world, document.pk)
    assert claim is not None
    assert (claim.completed_at, claim.error_code) == (None, "chaves_divergentes")
    assert default_storage.exists(moved)
    assert default_storage.exists(document.storage_key)


def test_the_scanner_keeps_a_quarantine_key_another_tenants_row_names(
    world: World,
) -> None:
    report = _emit(world)
    original = _original(world, report)
    with tenant_context(world.tenant_id):
        intake = DocumentIntake.objects.get(document=original)
    foreign = make_firm("axrm051-r5-foreign-copy", client_count=1)
    with tenant_context(foreign.tenant.pk):
        # A row of another tenant names this intake's quarantine copy.
        Document.objects.create(
            tenant=foreign.tenant,
            client=foreign.clients[0],
            storage_key=intake.quarantine_key,
            sha256=original.sha256,
            original_filename="alheio.pdf",
            content_type="application/pdf",
            byte_size=original.byte_size,
            uploaded_by=foreign.owner,
            acquisition_origin=AcquisitionOrigin.UPLOAD_STAFF,
            portal_visible=False,
        )
    world.scan()
    with tenant_context(world.tenant_id):
        assert DocumentIntake.objects.get(pk=intake.pk).state in CLEAN_STATES
    assert default_storage.exists(intake.quarantine_key)


def test_a_purge_between_the_copy_and_the_verdict_finds_the_key_locked(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    document, intake = _ordinary_pending(world, with_final=False)
    record = scanning.record_verdict
    purged: list[dict[str, int]] = []

    def purge() -> None:
        try:
            purged.append(
                purge_tenant(
                    world.tenant_id,
                    dry_run=False,
                    now=datetime(2030, 1, 1, tzinfo=SAO_PAULO),
                )
            )
        finally:
            connection.close()  # this thread's own connection

    def purge_in_the_gap(*args: object, **kwargs: object) -> scanning.ScanOutcome:
        # Runs after the copy and before the verdict, on another connection; the
        # purge never waits on a key lock, so the join returns at once.
        worker = threading.Thread(target=purge)
        worker.start()
        worker.join(timeout=60)
        assert not worker.is_alive()
        return record(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(scanning, "record_verdict", purge_in_the_gap)
    outcome = scanning.scan_intake(
        world.tenant_id, intake.pk, scanner=FakeScanner(), now=timezone.now()
    )
    assert len(purged) == 1
    assert _claim(world, document.pk) is not None
    assert outcome != scanning.ScanOutcome.VERIFIED
    with tenant_context(world.tenant_id):
        assert DocumentIntake.objects.get(pk=intake.pk).state not in CLEAN_STATES
    assert default_storage.exists(document.storage_key)
    assert default_storage.exists(intake.quarantine_key)


def test_the_fiscal_year_is_sao_paulos_whatever_the_active_zone(world: World) -> None:
    report = _emit(world)
    world.scan()
    current = world.reload(report)
    # January 1 in São Paulo, December 31 in a UTC-04:00 zone.
    current.emitted_at = datetime(2026, 1, 1, 3, 30, tzinfo=UTC)
    with tenant_context(world.tenant_id), timezone.override("America/Manaus"):
        anchors, gaps = serpro_retention._report_anchors(current)
    assert gaps == []
    assert [anchor.on for anchor in anchors] == [date(2026, 1, 1)]
    assert retention.fiscal_expiry(anchors) == datetime(2032, 1, 1, tzinfo=SAO_PAULO)


def _foreign_original(world: World, key: str) -> None:
    """Another tenant's held original naming `key`."""
    foreign = make_firm(f"axrm051-r6-{uuid4().hex[:8]}", client_count=1)
    with tenant_context(foreign.tenant.pk):
        Document.objects.create(
            tenant=foreign.tenant,
            client=foreign.clients[0],
            storage_key=key,
            sha256=hashlib.sha256(b"held elsewhere").hexdigest(),
            original_filename="alheio.pdf",
            content_type="application/pdf",
            byte_size=14,
            uploaded_by=foreign.owner,
            acquisition_origin=AcquisitionOrigin.UPLOAD_STAFF,
            portal_visible=False,
        )


def test_a_key_attached_after_the_ownership_lookup_is_checked_under_the_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    document, intake = _ordinary_pending(world, with_final=True)
    # A key of the copy's own kind (inside its quarantine prefix) that another
    # tenant's row holds: only the check under the lock can see it.
    held = default_storage.save(
        f"{posixpath.dirname(intake.quarantine_key)}/{uuid4().hex}",
        ContentFile(b"held"),
    )
    _foreign_original(world, held)
    lookup = retention.foreign_storage_keys
    calls: list[int] = []

    def then_reconciled(
        tenant_id: UUID, keys: Iterable[str], storage: Storage | None = None
    ) -> set[str]:
        found = lookup(tenant_id, keys, storage)
        if not calls:
            # After the sweep's lookup, a reconciliation points the intake at K.
            with tenant_context(world.tenant_id):
                DocumentIntake.objects.filter(pk=intake.pk).update(quarantine_key=held)
        calls.append(1)
        return found

    monkeypatch.setattr(
        "apps.cycles.retention_tasks.foreign_storage_keys", then_reconciled
    )
    purge_tenant(
        world.tenant_id, dry_run=False, now=datetime(2030, 1, 1, tzinfo=SAO_PAULO)
    )
    assert default_storage.exists(held)
    claim = _claim(world, document.pk)
    assert claim is not None
    assert (claim.completed_at, claim.error_code) == (None, "chave_compartilhada")


@pytest.mark.parametrize(
    ("restored", "spelling"),
    [
        ("here", "doubled"),
        ("elsewhere", "doubled"),
        ("here", "parent"),
        ("elsewhere", "parent"),
    ],
)
def test_two_spellings_of_one_file_are_one_key(
    world: World, restored: str, spelling: str
) -> None:
    document, intake = _ordinary_pending(world, with_final=True)
    # Inside the copy's own prefix, so only the row lookup can keep it (a short
    # name: a Document's key, spelled with `restored/../`, holds 128 characters).
    key = default_storage.save(
        f"{posixpath.dirname(intake.quarantine_key)}/{uuid4().hex[:16]}",
        ContentFile(b"held"),
    )
    # One side names the file another way (`a//b` or `a/x/../b`); storage reads
    # one file.
    spelled = (
        key.replace("/", "//", 1)
        if spelling == "doubled"
        else key.replace("/", "/restored/../", 1)
    )
    _foreign_original(world, spelled if restored == "elsewhere" else key)
    with tenant_context(world.tenant_id):
        DocumentIntake.objects.filter(pk=intake.pk).update(
            quarantine_key=key if restored == "elsewhere" else spelled
        )
    purge_tenant(
        world.tenant_id, dry_run=False, now=datetime(2030, 1, 1, tzinfo=SAO_PAULO)
    )
    assert default_storage.exists(key)
    assert _claim(world, document.pk) is None


@pytest.mark.parametrize("spelling", ["dotted", "absolute", "reentry"])
def test_one_lock_per_object_whatever_its_spelling(world: World, spelling: str) -> None:
    root = Path(default_storage.path("")).resolve()
    other_name = {
        "dotted": "originals//./r6.pdf",
        "absolute": str(root / "originals" / "r6.pdf"),
        "reentry": f"../{root.name}/originals/r6.pdf",
    }[spelling]
    other = connection.get_new_connection(connection.get_connection_params())
    try:
        other.autocommit = True
        lock_id = retention._key_lock_id("originals/r6.pdf")
        other.execute("SELECT pg_advisory_lock(%s)", [lock_id])
        with retention.storage_keys_locked([other_name], wait=False) as got:
            taken = got
        other.execute("SELECT pg_advisory_unlock(%s)", [lock_id])
    finally:
        other.close()
    assert taken is False


@pytest.mark.parametrize(
    "pause", ["copy-delete-fails", "final-delete-fails", "final-replaced"]
)
def test_cleanup_never_removes_a_copy_the_original_may_still_need(
    world: World, monkeypatch: pytest.MonkeyPatch, pause: str
) -> None:
    report = _emit(world)
    original = _original(world, report)
    with tenant_context(world.tenant_id):
        intake = DocumentIntake.objects.get(document=original)
    copy, final = intake.quarantine_key, original.storage_key
    discard = scanning._discard_quarantine
    real_delete = default_storage.delete
    disposed: list[str] = []  # what the purge itself deleted, in its key order

    def paused(ticket: scanning.ScanTicket, storage: object) -> None:
        # The scanner committed VERIFIED and pauses here, before its cleanup.
        if pause == "final-replaced":
            real_delete(final)
            default_storage.save(final, ContentFile(b"not the original"))
        else:
            failing = copy if pause == "copy-delete-fails" else final

            def delete(name: str) -> None:
                if name == failing:
                    message = "synthetic storage refusal"
                    raise OSError(message)
                disposed.append(name)
                real_delete(name)

            with monkeypatch.context() as patch:
                patch.setattr(default_storage, "delete", delete)
                purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))
        discard(ticket, storage)  # type: ignore[arg-type]

    monkeypatch.setattr(scanning, "_discard_quarantine", paused)
    world.scan()
    # The claim's key order follows the database collation over random names, so
    # the purge may reach either copy first. Whatever it did not delete itself is
    # still there after cleanup, and the original always keeps one copy.
    assert default_storage.exists(copy) is (copy not in disposed)
    assert default_storage.exists(final) is (final not in disposed)
    assert default_storage.exists(copy) or default_storage.exists(final)
    if pause != "final-replaced":
        claim = _claim(world, original.pk)
        assert claim is not None
        assert claim.completed_at is None


def test_cleanup_checks_other_tenants_under_the_copys_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _emit(world)
    with tenant_context(world.tenant_id):
        intake = DocumentIntake.objects.get(document=_original(world, report))
    locked = retention.storage_keys_locked

    @contextmanager
    def then_aliased(
        keys: Iterable[str], *, wait: bool, storage: Storage | None = None
    ) -> Iterator[bool]:
        if intake.quarantine_key in keys:
            _foreign_original(world, intake.quarantine_key)  # just before the lock
        with locked(keys, wait=wait, storage=storage) as got:
            yield got

    monkeypatch.setattr("apps.cycles.scanning.storage_keys_locked", then_aliased)
    world.scan()
    assert default_storage.exists(intake.quarantine_key)


@pytest.mark.parametrize("disposed", ["before-the-ticket", "after-the-ticket"])
def test_a_source_disposed_of_is_not_a_scanner_outage(
    world: World, monkeypatch: pytest.MonkeyPatch, disposed: str
) -> None:
    _document, intake = _ordinary_pending(world, with_final=False)
    late = intake.created_at + timedelta(hours=25)
    when = datetime(2030, 1, 1, tzinfo=SAO_PAULO)
    if disposed == "before-the-ticket":
        assert purge_tenant(world.tenant_id, dry_run=False, now=when)["purged"] == 1
        assert intake.pk not in scanning.due_intakes(world.tenant_id, late)
        assert scanning.claim_scan(world.tenant_id, intake.pk) is None
    else:
        run = scanning.run_scan

        def purged_first(*args: object, **kwargs: object) -> object:
            assert purge_tenant(world.tenant_id, dry_run=False, now=when)["purged"] == 1
            return run(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(scanning, "run_scan", purged_first)
    outcome = scanning.scan_intake(
        world.tenant_id, intake.pk, scanner=FakeScanner(), now=late
    )
    assert outcome == scanning.ScanOutcome.SKIPPED
    with tenant_context(world.tenant_id):
        assert DocumentIntake.objects.get(pk=intake.pk).state in PENDING_SCAN_STATES


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(hours=1), scanning.ScanOutcome.RETRY),
        (timedelta(hours=25), scanning.ScanOutcome.UNAVAILABLE),
    ],
)
def test_a_clean_verdict_certifies_only_the_bytes_it_scanned(
    world: World, age: timedelta, expected: scanning.ScanOutcome
) -> None:
    document, intake = _ordinary_pending(world, with_final=True)
    now = timezone.now()
    with tenant_context(world.tenant_id):
        # The model default reads the wall clock; the age is set from this one.
        DocumentIntake.objects.filter(pk=intake.pk).update(created_at=now - age)
        cycle = ServiceCycle.objects.get(pk=world.cycle.pk)
        request = create_request(
            world.termo.actor,
            cycle.pk,
            RequestFields("Original em verificação"),
            expected_version=cycle.version,
        )
        EvidenceLink.objects.create(
            tenant=world.termo.tenant,
            client=world.termo.client,
            request=request,
            document=document,
            origin="staff",
            submitted_by=world.termo.actor,
        )
    # Crash recovery: the final is the original; the copy is another benign PDF.
    default_storage.delete(intake.quarantine_key)
    default_storage.save(intake.quarantine_key, ContentFile(b"%PDF-1.7\n% other\n"))
    outcome = scanning.scan_intake(
        world.tenant_id, intake.pk, scanner=FakeScanner(), now=now
    )
    # Never certified; the mismatch takes the failure path and its 24-hour line.
    assert outcome == expected
    with tenant_context(world.tenant_id):
        current = DocumentIntake.objects.get(pk=intake.pk)
        problems = list(
            CycleException.objects.filter(subject_ref=intake.pk).values_list(
                "reason_code", "cycle_id", "state"
            )
        )
    if expected is scanning.ScanOutcome.RETRY:
        assert current.state in PENDING_SCAN_STATES
        assert problems == []
    else:
        assert (current.state, current.reason) == (
            IntakeState.SCAN_UNAVAILABLE,
            "conteudo_divergente_24h",
        )
        assert problems == [("verificacao_indisponivel", world.cycle.pk, "aberta")]


@pytest.mark.parametrize("reads", [8, -1])
def test_only_bytes_the_scanner_itself_read_are_certified(
    world: World, reads: int
) -> None:
    _document, intake = _ordinary_pending(world, with_final=False)
    ticket = scanning.claim_scan(world.tenant_id, intake.pk)
    assert ticket is not None

    class Reading:
        def scan(self, stream: scanning.ByteStream) -> scanning.Verdict:
            stream.read(reads)  # eight bytes, or everything
            return FakeScanner().scan(BytesIO(b""))

    try:
        verdict = scanning.run_scan(ticket, Reading(), default_storage)
        certified = not verdict.infected
    except scanning.ContentMismatch:
        certified = False
    # A short read is not certified: the unread suffix was never scanned.
    assert certified is (reads == -1)


def test_a_disposal_claimed_after_the_ticket_stops_the_copy(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    document, intake = _ordinary_pending(world, with_final=False)
    run = scanning.run_scan

    def claimed_meanwhile(*args: object, **kwargs: object) -> scanning.Verdict:
        # The ticket is out; a purge claims the original and fails to delete it.
        with monkeypatch.context() as patch:
            patch.setattr(default_storage, "delete", _refuse_delete)
            purge_tenant(
                world.tenant_id,
                dry_run=False,
                now=datetime(2030, 1, 1, tzinfo=SAO_PAULO),
            )
        return run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(scanning, "run_scan", claimed_meanwhile)
    outcome = scanning.scan_intake(
        world.tenant_id, intake.pk, scanner=FakeScanner(), now=timezone.now()
    )
    assert _claim(world, document.pk) is not None
    assert outcome == scanning.ScanOutcome.SKIPPED
    assert not default_storage.exists(document.storage_key)  # never copied
    with tenant_context(world.tenant_id):
        assert DocumentIntake.objects.get(pk=intake.pk).state == IntakeState.QUARANTINED


def _refuse_delete(key: str) -> None:
    message = "synthetic storage refusal"
    raise OSError(message)


def test_a_reconciliation_before_the_locks_never_deletes_the_old_key(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    document, intake = _ordinary_pending(world, with_final=True)
    when = datetime(2030, 1, 1, tzinfo=SAO_PAULO)
    with monkeypatch.context() as patch:
        patch.setattr(default_storage, "delete", _refuse_delete)
        purge_tenant(world.tenant_id, dry_run=False, now=when)
    assert _claim(world, document.pk) is not None  # open for {final, Q1}
    old = intake.quarantine_key
    dispose = retention_tasks._dispose

    def reconciled_first(*args: object, **kwargs: object) -> str:
        # After the retry compared the keys, before it takes their locks: the
        # intake moves to Q2 and a replacement is left at Q1.
        moved = default_storage.save(f"quarantine/{uuid4().hex}", ContentFile(b"Q2"))
        with tenant_context(world.tenant_id):
            DocumentIntake.objects.filter(pk=intake.pk).update(quarantine_key=moved)
        default_storage.delete(old)
        default_storage.save(old, ContentFile(b"replacement"))
        return dispose(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(retention_tasks, "_dispose", reconciled_first)
    purge_tenant(world.tenant_id, dry_run=False, now=when)
    assert default_storage.exists(old)
    with default_storage.open(old, "rb") as handle:
        assert handle.read() == b"replacement"
    claim = _claim(world, document.pk)
    assert claim is not None
    assert (claim.completed_at, claim.error_code) == (None, "chaves_divergentes")


def _mountinfo(*points: Path) -> bytes:
    """A `/proc/self/mountinfo` with these mount points, escaped as the kernel does."""
    lines = []
    for number, point in enumerate(points, start=20):
        raw = os.fsencode(point)
        for plain, escaped in (
            (b"\\", b"\\134"),
            (b" ", b"\\040"),
            (b"\t", b"\\011"),
            (b"\n", b"\\012"),
        ):
            raw = raw.replace(plain, escaped)
        lines.append(
            b"%d 1 8:1 / %s rw,relatime shared:1 - ext4 /dev/sda1 rw" % (number, raw)
        )
    return b"\n".join(lines) + b"\n"


@dataclass(frozen=True)
class _Layout:
    """What one layout case needs to set itself up."""

    world: World
    monkeypatch: pytest.MonkeyPatch
    root: Path
    name: str
    held: str
    copy: str


def _unrelated_hard_link(case: _Layout) -> tuple[str, str, str]:
    # A second name for this copy that no row spells: only the link count sees it.
    (case.root / "spare").mkdir(exist_ok=True)
    (case.root / "spare" / f"alias-{case.name}").hardlink_to(case.root / case.copy)
    return case.copy, case.held, "arquivo_com_vinculos"


def _unrelated_symlink(case: _Layout) -> tuple[str, str, str]:
    # An in-root directory link that no row spells: only the walk can see it.
    (case.root / "legacy").symlink_to(case.root / "originals", target_is_directory=True)
    return case.copy, case.held, "armazenamento_com_atalho"


def _symlink(case: _Layout, *, here_side: bool) -> tuple[str, str, str]:
    root, name, held = case.root, case.name, case.held
    (root / "legacy").symlink_to(root / "originals", target_is_directory=True)
    # One side names the held file through the in-root directory link.
    here, elsewhere = (
        (f"legacy/{name}", held) if here_side else (held, f"legacy/{name}")
    )
    _foreign_original(case.world, elsewhere)
    return here, held, "armazenamento_com_atalho"


def _hard_link(case: _Layout) -> tuple[str, str, str]:
    here = f"{posixpath.dirname(case.copy)}/{uuid4().hex}"  # inside its own prefix
    (case.root / here).parent.mkdir(parents=True, exist_ok=True)
    (case.root / here).hardlink_to(case.root / case.held)
    _foreign_original(case.world, case.held)
    return here, case.held, "arquivo_com_vinculos"


def _outside_root(case: _Layout) -> tuple[str, str, str]:
    outside = case.root.parent / f"outside-{case.name}"
    outside.write_bytes(b"held")
    return f"../{outside.name}", str(outside), "chave_fora_do_armazenamento"


def _sealed(case: _Layout) -> tuple[str, str, str]:
    # Searchable but not listable: the walk cannot see what is inside.
    (case.root / "sealed").mkdir()
    (case.root / "sealed" / "inner").write_bytes(b"unseen")
    (case.root / "sealed").chmod(0o300)
    _foreign_original(case.world, case.held)
    return case.copy, case.held, "armazenamento_nao_inspecionavel"


def _under_a_file(case: _Layout) -> tuple[str, str, str]:
    # A restored descriptor under a regular file, inside its own prefix.
    return f"{case.copy}/child", case.held, "armazenamento_nao_inspecionavel"


def _mounted(case: _Layout) -> tuple[str, str, str]:
    # Stand-in for a bind mount (which needs privileges): the kernel's mount
    # table, which the proof reads, lists a mount point under the root.
    (case.root / "mirror").mkdir()
    table = _mountinfo(Path("/"), case.root / "mirror")
    case.monkeypatch.setattr(retention, "_mount_table", lambda: table)
    _foreign_original(case.world, case.held)
    return case.copy, case.held, "armazenamento_com_montagem"


def _no_mount_table(case: _Layout) -> tuple[str, str, str]:
    def unreadable() -> bytes:
        message = "no /proc in this container"
        raise OSError(message)

    case.monkeypatch.setattr(retention, "_mount_table", unreadable)
    _foreign_original(case.world, case.held)
    return case.copy, case.held, "armazenamento_nao_inspecionavel"


def _malformed_mount_table(case: _Layout) -> tuple[str, str, str]:
    table = b"20 1 8:1 / / rw,relatime shared:1 ext4 /dev/sda1 rw\n"  # no `-`
    case.monkeypatch.setattr(retention, "_mount_table", lambda: table)
    _foreign_original(case.world, case.held)
    return case.copy, case.held, "armazenamento_nao_inspecionavel"


def _case_blind(case: _Layout) -> tuple[str, str, str]:
    # Stand-in for a case-folding directory: every case of the name opens the one
    # entry, so the probe sees it whichever name the directory lists first.
    (case.root / "cased").mkdir()
    (case.root / "cased" / "Case.pdf").write_bytes(b"one entry")
    for other in ("case.pdf", "CASE.PDF"):
        (case.root / "cased" / other).hardlink_to(case.root / "cased" / "Case.pdf")
    # The listing order is the filesystem's; list lowercase first, the order in
    # which a partial stand-in would go unseen, so no run depends on it.
    walk = os.walk

    def lowercase_first(
        top: Path, onerror: Callable[[OSError], object] | None = None
    ) -> Iterator[tuple[str, list[str], list[str]]]:
        for parent, directories, files in walk(top, onerror=onerror):
            directories.sort(reverse=True)
            yield parent, directories, sorted(files, reverse=True)

    case.monkeypatch.setattr(os, "walk", lowercase_first)
    _foreign_original(case.world, case.held)
    return case.copy, case.held, "armazenamento_sem_distincao_de_caixa"


def _fifo(case: _Layout) -> tuple[str, str, str]:
    here = f"{posixpath.dirname(case.copy)}/{uuid4().hex}"  # inside its own prefix
    (case.root / here).parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(case.root / here)  # one link, no writer: opening it would block
    return here, case.held, "arquivo_nao_regular"


_LAYOUTS: dict[str, Callable[[_Layout], tuple[str, str, str]]] = {
    "symlink-here": lambda case: _symlink(case, here_side=True),
    "symlink-elsewhere": lambda case: _symlink(case, here_side=False),
    "symlink-unrelated": _unrelated_symlink,
    "hard-link-unrelated": _unrelated_hard_link,
    "hard-link": _hard_link,
    "outside-root": _outside_root,
    "sealed-directory": _sealed,
    "not-a-directory": _under_a_file,
    "mount-under-root": _mounted,
    "mount-table-unreadable": _no_mount_table,
    "mount-table-malformed": _malformed_mount_table,
    "case-blind-directory": _case_blind,
    "fifo": _fifo,
}


@pytest.mark.parametrize(
    "layout",
    [
        "symlink-here",
        "symlink-elsewhere",
        "symlink-unrelated",
        "hard-link-unrelated",
        "hard-link",
        "outside-root",
        "sealed-directory",
        "not-a-directory",
        "mount-under-root",
        "mount-table-unreadable",
        "mount-table-malformed",
        "case-blind-directory",
        "fifo",
    ],
)
def test_a_file_with_names_no_lookup_sees_is_never_deleted(
    world: World, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    document, intake = _ordinary_pending(world, with_final=True)
    root = Path(default_storage.path("")).resolve()
    name = f"{uuid4().hex}.pdf"
    held = default_storage.save(f"originals/{name}", ContentFile(b"held"))
    here, held, expected = _LAYOUTS[layout](
        _Layout(world, monkeypatch, root, name, held, intake.quarantine_key)
    )
    with tenant_context(world.tenant_id):
        DocumentIntake.objects.filter(pk=intake.pk).update(quarantine_key=here)
    try:
        purge_tenant(
            world.tenant_id, dry_run=False, now=datetime(2030, 1, 1, tzinfo=SAO_PAULO)
        )
        crashed = False
    except (SuspiciousFileOperation, OSError):
        crashed = True  # an uncaught refusal stops the sweep: not a recorded one
    finally:
        if layout == "sealed-directory":
            (root / "sealed").chmod(0o700)
    assert crashed is False
    assert (Path(held) if layout == "outside-root" else root / held).exists()
    if layout == "fifo":
        assert (root / here).exists()
    claim = _claim(world, document.pk)
    if layout in {"symlink-here", "symlink-elsewhere", "hard-link", "outside-root"}:
        # Refused before any claim: either one side names the held file through
        # the link (physical identity, the inode, is the held file's own), or
        # the key leaves its own prefix (`outside-root` leaves the root too). The
        # walk's and the link count's own refusals are witnessed by
        # symlink-unrelated and hard-link-unrelated.
        assert claim is None
        return
    assert claim is not None
    assert (claim.completed_at, claim.error_code) == (None, expected)


@pytest.mark.parametrize(
    "spelling", ["doubled", "symlink", "absolute", "mount", "same-inode"]
)
def test_cleanup_never_removes_a_copy_that_is_the_final(
    world: World, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    document, intake = _ordinary_pending(world, with_final=True)
    root = Path(default_storage.path("")).resolve()
    final = document.storage_key
    if spelling == "doubled":
        copy = final.replace("/", "//", 1)
    elif spelling == "absolute":
        copy = str(root / final)  # the same file, spelled from the root
    elif spelling == "mount":
        # Stand-in for `mirror/` bind-mounted onto `originals/`: the mount table
        # lists it; the copy here is a separate file the proof must still keep.
        (root / "mirror").mkdir()
        copy = f"mirror/{Path(final).name}"
        (root / copy).write_bytes((root / final).read_bytes())
        table = _mountinfo(Path("/"), root / "mirror")
        monkeypatch.setattr(retention, "_mount_table", lambda: table)
    elif spelling == "same-inode":
        # An alias the shared ownership check misses: the inode comparison alone
        # decides.
        (root / "spare").mkdir()
        copy = f"spare/{Path(final).name}"
        (root / copy).hardlink_to(root / final)
        monkeypatch.setattr(scanning, "ownership_refusal", lambda *a, **k: "")
    else:
        (root / "legacy").symlink_to(root / "originals", target_is_directory=True)
        copy = final.replace("originals/", "legacy/", 1)
    default_storage.delete(intake.quarantine_key)
    with tenant_context(world.tenant_id):
        DocumentIntake.objects.filter(pk=intake.pk).update(quarantine_key=copy)
    outcome = scanning.scan_intake(
        world.tenant_id, intake.pk, scanner=FakeScanner(), now=timezone.now()
    )
    assert outcome == scanning.ScanOutcome.VERIFIED
    assert (root / final).exists()
    assert (
        physical_identity(copy, default_storage)
        == physical_identity(final, default_storage)
        or (root / copy).exists()
    )


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(hours=1), scanning.ScanOutcome.RETRY),
        (timedelta(hours=25), scanning.ScanOutcome.UNAVAILABLE),
    ],
)
def test_a_final_that_is_not_the_original_is_never_certified(
    world: World, age: timedelta, expected: scanning.ScanOutcome
) -> None:
    document, intake = _ordinary_pending(world, with_final=True)
    now = timezone.now()
    with tenant_context(world.tenant_id):
        DocumentIntake.objects.filter(pk=intake.pk).update(created_at=now - age)
    # The copy is the original; the final already stored is another PDF.
    default_storage.delete(document.storage_key)
    default_storage.save(document.storage_key, ContentFile(b"%PDF-1.7\n% other\n"))
    try:
        outcome: object = scanning.scan_intake(
            world.tenant_id, intake.pk, scanner=FakeScanner(), now=now
        )
    except scanning.IntegrityMismatch:
        outcome = "raised"
    assert outcome == expected
    with tenant_context(world.tenant_id):
        current = DocumentIntake.objects.get(pk=intake.pk)
    if expected is scanning.ScanOutcome.RETRY:
        assert current.state in PENDING_SCAN_STATES
    else:
        assert current.reason == "conteudo_divergente_24h"


def test_an_infected_replacement_is_never_the_originals_verdict(world: World) -> None:
    _document, intake = _ordinary_pending(world, with_final=True)
    default_storage.delete(intake.quarantine_key)
    default_storage.save(intake.quarantine_key, ContentFile(scanning.EICAR))
    outcome = scanning.scan_intake(
        world.tenant_id, intake.pk, scanner=FakeScanner(), now=timezone.now()
    )
    assert outcome != scanning.ScanOutcome.INFECTED
    with tenant_context(world.tenant_id):
        assert DocumentIntake.objects.get(pk=intake.pk).state in PENDING_SCAN_STATES
    assert default_storage.exists(intake.quarantine_key)  # suspect copy kept


def _foreign_copy(world: World, key: str) -> None:
    """Another tenant's intake whose copy names `key` (an absolute key fits here)."""
    foreign = make_firm(f"axrm051-r8-{uuid4().hex[:8]}", client_count=1)
    with tenant_context(foreign.tenant.pk):
        document = Document.objects.create(
            tenant=foreign.tenant,
            client=foreign.clients[0],
            storage_key=f"originals/{uuid4().hex}.pdf",
            sha256=hashlib.sha256(b"held elsewhere").hexdigest(),
            original_filename="alheio.pdf",
            content_type="application/pdf",
            byte_size=14,
            uploaded_by=foreign.owner,
            acquisition_origin=AcquisitionOrigin.UPLOAD_STAFF,
            portal_visible=False,
        )
        DocumentIntake.objects.create(
            tenant=foreign.tenant,
            client=foreign.clients[0],
            document=document,
            state=IntakeState.VERIFIED,
            verdict=ScanVerdict.CLEAN,
            quarantine_key=key,
            scanned_at=timezone.now(),
            created_at=timezone.now(),
        )


@pytest.mark.parametrize(
    "spelled", ["absolute-here", "reentry-here", "absolute-elsewhere"]
)
def test_an_absolute_or_reentering_spelling_is_the_same_key(
    world: World, spelled: str
) -> None:
    document, intake = _ordinary_pending(world, with_final=True)
    root = Path(default_storage.path("")).resolve()
    # Inside the copy's own prefix, so only the row lookup can keep it.
    key = default_storage.save(
        f"{posixpath.dirname(intake.quarantine_key)}/{uuid4().hex}",
        ContentFile(b"held"),
    )
    absolute, reentry = str(root / key), f"../{root.name}/{key}"
    if spelled == "absolute-elsewhere":
        _foreign_copy(world, absolute)  # the held row is the one spelled oddly
        here = key
    else:
        _foreign_original(world, key)
        here = absolute if spelled == "absolute-here" else reentry
    with tenant_context(world.tenant_id):
        DocumentIntake.objects.filter(pk=intake.pk).update(quarantine_key=here)
    purge_tenant(
        world.tenant_id, dry_run=False, now=datetime(2030, 1, 1, tzinfo=SAO_PAULO)
    )
    assert (root / key).exists()
    claim = _claim(world, document.pk)
    assert claim is None or claim.completed_at is None


def _scanned_in_place(world: World, stored: bytes, original: bytes) -> DocumentIntake:
    """A pending intake read from its final key (no quarantine copy)."""
    termo = world.termo
    with tenant_context(world.tenant_id):
        document = Document.objects.create(
            tenant=termo.tenant,
            client=termo.client,
            storage_key=default_storage.save(
                f"originals/{uuid4().hex}.pdf", ContentFile(stored)
            ),
            sha256=hashlib.sha256(original).hexdigest(),
            original_filename="direto.pdf",
            content_type="application/pdf",
            byte_size=len(original),
            uploaded_by=termo.actor,
            acquisition_origin=AcquisitionOrigin.UPLOAD_STAFF,
            portal_visible=False,
        )
        return DocumentIntake.objects.create(
            tenant=termo.tenant,
            client=termo.client,
            document=document,
            state=IntakeState.RECEIVED,
            created_at=timezone.now(),
        )


def test_a_scan_that_stops_where_the_original_ends_is_not_certified(
    world: World,
) -> None:
    original = b"%PDF-1.7\n% the expected original\n%%EOF\n"
    intake = _scanned_in_place(world, original + b"% one more line\n", original)

    class StopsAtTheOriginal:
        def scan(self, stream: scanning.ByteStream) -> scanning.Verdict:
            stream.read(len(original))  # exactly the expected bytes, no further
            return FakeScanner().scan(BytesIO(b""))

    outcome = scanning.scan_intake(
        world.tenant_id, intake.pk, scanner=StopsAtTheOriginal(), now=timezone.now()
    )
    assert outcome != scanning.ScanOutcome.VERIFIED
    with tenant_context(world.tenant_id):
        assert DocumentIntake.objects.get(pk=intake.pk).state in PENDING_SCAN_STATES


def test_a_file_scanned_in_place_is_checked_again_under_its_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = b"%PDF-1.7\n% scanned in place\n%%EOF\n"
    intake = _scanned_in_place(world, original, original)
    with tenant_context(world.tenant_id):
        key = DocumentIntake.objects.get(pk=intake.pk).document.storage_key
    run = scanning.run_scan

    def replaced_after_the_read(*args: object, **kwargs: object) -> object:
        verdict = run(*args, **kwargs)  # type: ignore[arg-type]
        # A restore replaces the serving file after the scan read it.
        default_storage.delete(key)
        default_storage.save(key, ContentFile(b"%PDF-1.7\n% restored other\n"))
        return verdict

    monkeypatch.setattr(scanning, "run_scan", replaced_after_the_read)
    outcome = scanning.scan_intake(
        world.tenant_id, intake.pk, scanner=FakeScanner(), now=timezone.now()
    )
    assert outcome != scanning.ScanOutcome.VERIFIED
    with tenant_context(world.tenant_id):
        assert DocumentIntake.objects.get(pk=intake.pk).state in PENDING_SCAN_STATES


def test_a_verdict_on_a_read_that_stops_early_is_never_the_originals(
    world: World,
) -> None:
    original = b"%PDF-1.7\n% the expected original\n%%EOF\n"
    intake = _scanned_in_place(world, original + b"% unread suffix\n", original)

    class FlagsWhatItRead:
        def scan(self, stream: scanning.ByteStream) -> scanning.Verdict:
            stream.read(len(original))  # stops where the original would end
            return FakeScanner().scan(BytesIO(scanning.EICAR))  # an infected verdict

    outcome = scanning.scan_intake(
        world.tenant_id, intake.pk, scanner=FlagsWhatItRead(), now=timezone.now()
    )
    # No promotion re-reads the file on this path: only the EOF check keeps the
    # verdict of an incomplete read off the original.
    assert outcome != scanning.ScanOutcome.INFECTED
    with tenant_context(world.tenant_id):
        assert DocumentIntake.objects.get(pk=intake.pk).state in PENDING_SCAN_STATES


@pytest.mark.parametrize("spelled", ["configured-root", "real-root"])
def test_a_symlinked_root_and_its_target_are_one_root(
    world: World, settings: SettingsWrapper, spelled: str
) -> None:
    document, intake = _ordinary_pending(world, with_final=True)
    real = Path(default_storage.path("")).resolve()
    key = default_storage.save(f"originals/{uuid4().hex}.pdf", ContentFile(b"held"))
    configured = real.parent / f"media-link-{uuid4().hex[:8]}"
    configured.symlink_to(real, target_is_directory=True)
    try:
        settings.MEDIA_ROOT = str(configured)  # `/srv/media` -> `/data/pilot`
        # A held tenant's restored copy names the file from the configured root,
        # or from the real root, which the backend will not address.
        held = configured / key if spelled == "configured-root" else real / key
        _foreign_copy(world, str(held))
        with tenant_context(world.tenant_id):
            DocumentIntake.objects.filter(pk=intake.pk).update(quarantine_key=key)
        purge_tenant(
            world.tenant_id, dry_run=False, now=datetime(2030, 1, 1, tzinfo=SAO_PAULO)
        )
        assert (real / key).exists()
        claim = _claim(world, document.pk)
        assert claim is None or claim.completed_at is None
        assert physical_identity(str(held), default_storage) == physical_identity(
            key, default_storage
        )
    finally:
        configured.unlink()


def test_a_saved_key_spelled_canonically_is_the_key_requested(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _document, intake = _ordinary_pending(world, with_final=False)
    name = f"{uuid4().hex}.pdf"
    with tenant_context(world.tenant_id):
        Document.objects.filter(pk=intake.document_id).update(
            storage_key=f"originals//{name}"
        )
    save = default_storage.save

    def normalising(key: str, content: object, **kwargs: object) -> str:
        return save(posixpath.normpath(key), content, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(default_storage, "save", normalising)
    try:
        outcome: object = scanning.scan_intake(
            world.tenant_id, intake.pk, scanner=FakeScanner(), now=timezone.now()
        )
    except RuntimeError:
        outcome = "raised"
    assert outcome == scanning.ScanOutcome.VERIFIED
    assert default_storage.exists(f"originals/{name}")


def test_a_quarantine_key_that_is_not_a_regular_file_is_never_opened(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _document, intake = _ordinary_pending(world, with_final=False)
    root = Path(default_storage.path("")).resolve()
    fifo = f"quarantine/{uuid4().hex}"
    (root / fifo).parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(root / fifo)
    with tenant_context(world.tenant_id):
        DocumentIntake.objects.filter(pk=intake.pk).update(quarantine_key=fifo)
    opened: list[str] = []
    real_open = default_storage.open

    def recording(name: str, mode: str = "rb") -> object:
        opened.append(name)
        if name == fifo:
            return BytesIO(b"")  # never block the test, even on the old code
        return real_open(name, mode)

    monkeypatch.setattr(default_storage, "open", recording)
    real_os_open = os.open

    def recording_os(path: str, flags: int, *rest: int) -> int:
        opened.append(os.fspath(path))
        return real_os_open(path, flags, *rest)

    # The one safe open's descriptor call: a FIFO must not even reach it.
    monkeypatch.setattr(retention, "_open", recording_os, raising=False)
    outcome = scanning.scan_intake(
        world.tenant_id, intake.pk, scanner=FakeScanner(), now=timezone.now()
    )
    assert fifo not in opened
    assert default_storage.path(fifo) not in opened
    assert outcome != scanning.ScanOutcome.VERIFIED


@pytest.mark.parametrize(
    "root_name",
    [
        b"caf\xc3\xa9",
        b"with space",
        b"r\xffoot",
        b"vertical\x0btab",
        b"form\x0cfeed",
        b"carriage\rreturn",
        b"no\xc2\xa0break",
    ],
    ids=[
        "accented",
        "space",
        "not-utf8",
        "vertical-tab",
        "form-feed",
        "carriage-return",
        "no-break-space",
    ],
)
def test_a_mount_under_any_root_name_is_seen(
    world: World,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    root_name: bytes,
) -> None:
    root = Path(os.fsdecode(os.fsencode(tmp_path) + b"/" + root_name))
    root.mkdir()
    settings.MEDIA_ROOT = str(root)
    document, _intake = _ordinary_pending(world, with_final=True)
    (root / "mirror").mkdir()
    table = _mountinfo(
        Path("/"), root / "mirror"
    )  # the name exactly as the kernel lists it
    monkeypatch.setattr(retention, "_mount_table", lambda: table)
    purge_tenant(
        world.tenant_id, dry_run=False, now=datetime(2030, 1, 1, tzinfo=SAO_PAULO)
    )
    claim = _claim(world, document.pk)
    assert claim is not None
    assert (claim.completed_at, claim.error_code) == (
        None,
        "armazenamento_com_montagem",
    )
    assert default_storage.exists(document.storage_key)


def test_a_case_probe_that_cannot_answer_proves_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    document, _intake = _ordinary_pending(world, with_final=True)

    def unanswerable(self: Path, other: Path) -> bool:
        message = "I/O error"
        raise OSError(5, message)

    monkeypatch.setattr(Path, "samefile", unanswerable)
    purge_tenant(
        world.tenant_id, dry_run=False, now=datetime(2030, 1, 1, tzinfo=SAO_PAULO)
    )
    claim = _claim(world, document.pk)
    assert claim is not None
    assert (claim.completed_at, claim.error_code) == (
        None,
        "armazenamento_nao_inspecionavel",
    )


def test_a_root_reentered_through_its_link_is_the_same_root(
    world: World, settings: SettingsWrapper, tmp_path: Path
) -> None:
    _document, intake = _ordinary_pending(world, with_final=True)
    real = Path(default_storage.path("")).resolve()
    key = default_storage.save(f"originals/{uuid4().hex}.pdf", ContentFile(b"held"))
    # The link and its target live in different parents (`/srv/media` ->
    # `/data/pilot`): `../media/...` re-enters only through the configured root.
    links = tmp_path.parent / f"links-{uuid4().hex[:8]}"
    links.mkdir()
    (links / "media").symlink_to(real, target_is_directory=True)
    try:
        settings.MEDIA_ROOT = str(links / "media")
        _foreign_copy(world, f"../media/{key}")
        with tenant_context(world.tenant_id):
            DocumentIntake.objects.filter(pk=intake.pk).update(quarantine_key=key)
        purge_tenant(
            world.tenant_id, dry_run=False, now=datetime(2030, 1, 1, tzinfo=SAO_PAULO)
        )
        assert (real / key).exists()
        assert physical_identity(
            f"../media/{key}", default_storage
        ) == physical_identity(key, default_storage)
    finally:
        (links / "media").unlink()
        links.rmdir()


def _guarded(
    call: Callable[[], object], unblock: Path | None = None
) -> tuple[bool, object]:
    """Run `call` on a thread; report whether it blocked (then unblock a FIFO)."""
    result: list[object] = []

    def run() -> None:
        try:
            result.append(call())
        except Exception as error:  # noqa: BLE001 - the outcome is asserted
            result.append(error)
        finally:
            connection.close()

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(timeout=10)
    blocked = worker.is_alive()
    if blocked and unblock is not None:
        writer = os.open(
            unblock, os.O_WRONLY | os.O_NONBLOCK
        )  # lets the reader see EOF
        os.close(writer)
    worker.join(timeout=30)
    return blocked, (result[0] if result else None)


def test_a_fifo_at_the_final_key_never_blocks_promotion(world: World) -> None:
    document, intake = _ordinary_pending(world, with_final=False)
    root = Path(default_storage.path("")).resolve()
    (root / document.storage_key).parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(root / document.storage_key)  # an existing "final" with no writer
    blocked, outcome = _guarded(
        lambda: scanning.scan_intake(
            world.tenant_id, intake.pk, scanner=FakeScanner(), now=timezone.now()
        ),
        unblock=root / document.storage_key,
    )
    assert blocked is False
    assert outcome != scanning.ScanOutcome.VERIFIED
    with tenant_context(world.tenant_id):
        assert DocumentIntake.objects.get(pk=intake.pk).state in PENDING_SCAN_STATES


@pytest.mark.parametrize("swap", ["fifo", "fifo-after-lstat", "symlink-after-lstat"])
def test_a_stored_object_is_read_only_as_the_regular_file_it_was(
    world: World, monkeypatch: pytest.MonkeyPatch, swap: str
) -> None:
    root = Path(default_storage.path("")).resolve()
    key = f"spare/{uuid4().hex}"
    (root / "spare").mkdir(exist_ok=True)
    regular = root / "spare" / f"{uuid4().hex}.pdf"
    regular.write_bytes(b"regular")
    if swap == "symlink-after-lstat":
        (root / key).symlink_to(regular)
        target = regular
    else:
        os.mkfifo(root / key)
        target = regular
    if swap != "fifo":
        # The name is checked first, then swapped before it is opened.
        seen = target.stat()
        monkeypatch.setattr(retention, "_lstat", lambda path: seen)
    blocked, opened = _guarded(
        lambda: retention.open_stored(default_storage, key), unblock=root / key
    )
    if not isinstance(opened, BaseException) and opened is not None:
        opened.close()  # type: ignore[attr-defined]
    assert blocked is False
    assert isinstance(opened, OSError)


class _NameLiteral(Storage):
    """A remote object store whose names mean only themselves."""

    def __init__(
        self, objects: dict[str, bytes], behaviour: str, answer: str = ""
    ) -> None:
        self.objects = dict(objects)
        self.behaviour = behaviour
        self.answer = answer

    def _open(self, name: str, mode: str = "rb") -> File[bytes]:
        if name not in self.objects:
            raise FileNotFoundError(name)
        return ContentFile(self.objects[name], name=name)

    def _save(self, name: str, content: File[bytes]) -> str:
        payload = content.read()
        if self.behaviour == "stores-other":
            self.objects[f"{name}.alt"] = payload
            return name  # answers the name it never stored
        self.objects[name] = payload
        if self.answer:
            return self.answer  # stored as asked, but answers another key
        return f"{name}.alt" if self.behaviour == "answers-other" else name

    def exists(self, name: str) -> bool:
        return name in self.objects

    def delete(self, name: str) -> None:
        self.objects.pop(name, None)

    def size(self, name: str) -> int:
        return len(self.objects[name])


def test_cleanup_decides_with_the_storage_that_deletes(
    world: World, tmp_path: Path
) -> None:
    document, intake = _ordinary_pending(world, with_final=False)
    other = FileSystemStorage(location=str(tmp_path / f"second-{uuid4().hex[:8]}"))
    with default_storage.open(intake.quarantine_key, "rb") as handle:
        other.save(intake.quarantine_key, ContentFile(handle.read()))
    copy = Path(other.path(intake.quarantine_key)).resolve()
    # A held tenant names the scanner's copy in the second storage absolutely.
    _foreign_copy(world, str(copy))
    outcome = scanning.scan_intake(
        world.tenant_id,
        intake.pk,
        scanner=FakeScanner(),
        now=timezone.now(),
        storage=other,
    )
    assert outcome == scanning.ScanOutcome.VERIFIED
    # The final is the file the row names, written through the rows' storage; the
    # copy in the other storage is never compared across, so it is kept.
    assert default_storage.exists(document.storage_key)
    assert copy.exists()


@pytest.mark.parametrize("spelled", ["absolute", "relative"])
def test_one_lock_per_object_in_the_storage_that_holds_it(
    world: World, tmp_path: Path, spelled: str
) -> None:
    other = FileSystemStorage(location=str(tmp_path / f"locks-{uuid4().hex[:8]}"))
    absolute = (
        str(Path(other.location).resolve() / "originals" / "r10.pdf")
        if spelled == "absolute"
        else "originals/r10.pdf"  # only the second storage's root resolves it
    )
    held = connection.get_new_connection(connection.get_connection_params())
    try:
        held.autocommit = True
        lock_id = retention._key_lock_id("originals/r10.pdf", other)
        held.execute("SELECT pg_advisory_lock(%s)", [lock_id])
        with retention.storage_keys_locked(
            [absolute], wait=False, storage=other
        ) as got:
            taken = got
        held.execute("SELECT pg_advisory_unlock(%s)", [lock_id])
    finally:
        held.close()
    assert taken is False


def _swept(world: World) -> BaseException | None:
    """Run one sweep; return what escaped it, if anything did."""
    try:
        purge_tenant(
            world.tenant_id, dry_run=False, now=datetime(2030, 1, 1, tzinfo=SAO_PAULO)
        )
    except Exception as error:  # noqa: BLE001 - the escape itself is asserted
        return error
    return None


def test_a_spelling_the_backend_refuses_is_never_deleted_and_the_sweep_goes_on(
    world: World, settings: SettingsWrapper
) -> None:
    refused, _ = _ordinary_pending(world, with_final=True)
    other, _ = _ordinary_pending(world, with_final=True)
    real = Path(default_storage.path("")).resolve()
    configured = real.parent / f"media-link-{uuid4().hex[:8]}"
    configured.symlink_to(real, target_is_directory=True)
    # `/srv/media` -> `/data/pilot`: the row spells its final from the real root,
    # which the backend (rooted at the link) refuses to address.
    with tenant_context(world.tenant_id):
        Document.objects.filter(pk=refused.pk).update(
            storage_key=str(real / refused.storage_key)
        )
    try:
        settings.MEDIA_ROOT = str(configured)
        escaped = _swept(world)
    finally:
        configured.unlink()
    assert escaped is None
    claim = _claim(world, refused.pk)
    assert claim is not None
    assert (claim.completed_at, claim.error_code) == (None, "chave_nao_enderecavel")
    assert (real / refused.storage_key).exists()
    reached = _claim(world, other.pk)
    assert reached is not None
    assert reached.completed_at is not None


def test_one_failing_backend_call_refuses_only_its_candidate(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = _ordinary_pending(world, with_final=True)
    second, _ = _ordinary_pending(world, with_final=True)
    real_delete = default_storage.delete

    def refusing(name: str) -> None:
        if name == first.storage_key:
            message = "the backend refused this name"
            raise ValueError(message)
        real_delete(name)

    monkeypatch.setattr(default_storage, "delete", refusing)
    assert _swept(world) is None
    one = _claim(world, first.pk)
    assert one is not None
    assert (one.completed_at, one.error_code) == (None, "falha_inesperada")
    assert default_storage.exists(first.storage_key)
    two = _claim(world, second.pk)
    assert two is not None
    assert two.completed_at is not None


def test_cleanup_sees_a_held_owner_through_a_nested_root(world: World) -> None:
    _document, intake = _ordinary_pending(world, with_final=False)
    root = Path(default_storage.path("")).resolve()
    sub = f"pilot-{uuid4().hex[:8]}"
    nested = FileSystemStorage(location=str(root / sub))  # `/srv/media/pilot`
    with default_storage.open(intake.quarantine_key, "rb") as handle:
        nested.save(intake.quarantine_key, ContentFile(handle.read()))
    # A held tenant's row, written through the default root, names the very file
    # the scanner reaches as `quarantine/...` under its nested root.
    _foreign_copy(world, f"{sub}/{intake.quarantine_key}")
    outcome = scanning.scan_intake(
        world.tenant_id,
        intake.pk,
        scanner=FakeScanner(),
        now=timezone.now(),
        storage=nested,
    )
    assert outcome == scanning.ScanOutcome.VERIFIED
    assert nested.exists(intake.quarantine_key)


def test_one_lock_per_file_across_nested_roots() -> None:
    root = Path(default_storage.path("")).resolve()
    nested = FileSystemStorage(location=str(root / "pilot"))
    assert retention._key_lock_id(
        "pilot/quarantine/r11.pdf", default_storage
    ) == retention._key_lock_id("quarantine/r11.pdf", nested)


def test_rows_count_when_their_storage_cannot_be_compared(world: World) -> None:
    _ordinary_pending(world, with_final=False)
    bucket = _NameLiteral({}, "faithful")
    with tenant_context(world.tenant_id):
        assert retention.storage_key_aliased(
            [f"originals/{uuid4().hex}.pdf"], storage=bucket
        )


def test_a_final_created_meanwhile_is_never_written_over(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    document, intake = _ordinary_pending(world, with_final=False)
    final = Path(default_storage.path(document.storage_key))
    real_make = scanning._make_parents

    def then_created(storage: FileSystemStorage, directory: Path) -> None:
        real_make(storage, directory)
        if directory == final.parent and not final.exists():
            # Another owner's file appears between the check and the create.
            final.write_bytes(b"held by another owner")

    monkeypatch.setattr(scanning, "_make_parents", then_created, raising=False)
    outcome = scanning.scan_intake(
        world.tenant_id, intake.pk, scanner=FakeScanner(), now=timezone.now()
    )
    assert outcome != scanning.ScanOutcome.VERIFIED
    assert final.read_bytes() == b"held by another owner"


def _promote_remotely(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    intake: DocumentIntake,
    bucket: _NameLiteral,
) -> object:
    """Scan with a remote store that, as in production, holds the rows' objects."""
    monkeypatch.setattr(retention, "rows_storage", lambda: bucket, raising=False)
    monkeypatch.setattr(scanning, "rows_storage", lambda: bucket, raising=False)
    return scanning.scan_intake(
        world.tenant_id,
        intake.pk,
        scanner=FakeScanner(),
        now=timezone.now(),
        storage=bucket,
    )


def _document_key(world: World, intake: DocumentIntake) -> str:
    with tenant_context(world.tenant_id):
        return Document.objects.get(pk=intake.document_id).storage_key


def _remote_bucket(
    intake: DocumentIntake, behaviour: str, **more: bytes
) -> _NameLiteral:
    with default_storage.open(intake.quarantine_key, "rb") as handle:
        return _NameLiteral({intake.quarantine_key: handle.read(), **more}, behaviour)


def test_a_remote_promotion_lands_on_a_new_key_and_leaves_the_held_object(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _document, intake = _ordinary_pending(world, with_final=False)
    held = f"originals/{uuid4().hex}.pdf"
    requested = held.replace("/", "//", 1)  # a spelling of another owner's object
    with tenant_context(world.tenant_id):
        Document.objects.filter(pk=intake.document_id).update(storage_key=requested)
    bucket = _remote_bucket(intake, "faithful", **{held: b"held by another owner"})
    original = bucket.objects[intake.quarantine_key]
    outcome = _promote_remotely(world, monkeypatch, intake, bucket)
    assert outcome == scanning.ScanOutcome.VERIFIED
    final = _document_key(world, intake)
    assert final not in {requested, held}
    assert final.startswith("originals/")
    assert bucket.objects[final] == original
    assert bucket.objects[held] == b"held by another owner"


@pytest.mark.parametrize("in_use", ["object-there", "row-names-it"])
def test_an_allocated_key_already_in_use_is_refused_with_nothing_deleted(
    world: World, monkeypatch: pytest.MonkeyPatch, in_use: str
) -> None:
    _document, intake = _ordinary_pending(world, with_final=False)
    requested = _document_key(world, intake)
    taken = f"originals/{uuid4().hex}.pdf"
    extra = {taken: b"someone else's"} if in_use == "object-there" else {}
    bucket = _remote_bucket(intake, "faithful", **extra)
    if in_use == "row-names-it":
        _foreign_original(world, taken)
    before = dict(bucket.objects)
    monkeypatch.setattr(
        scanning, "_allocate_final_key", lambda requested: taken, raising=False
    )
    outcome = _promote_remotely(world, monkeypatch, intake, bucket)
    assert outcome != scanning.ScanOutcome.VERIFIED
    assert bucket.objects == before
    assert _document_key(world, intake) == requested


@pytest.mark.parametrize("behaviour", ["answers-other", "stores-other"])
def test_a_remote_save_under_another_name_is_not_the_original(
    world: World, monkeypatch: pytest.MonkeyPatch, behaviour: str
) -> None:
    _document, intake = _ordinary_pending(world, with_final=False)
    requested = _document_key(world, intake)
    outcome = _promote_remotely(
        world, monkeypatch, intake, _remote_bucket(intake, behaviour)
    )
    assert outcome != scanning.ScanOutcome.VERIFIED
    assert _document_key(world, intake) == requested


def test_a_refused_save_never_deletes_the_sole_copy(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _document, intake = _ordinary_pending(world, with_final=False)
    bucket = _remote_bucket(intake, "faithful")
    bucket.answer = intake.quarantine_key  # the save answers the sole copy's key
    outcome = _promote_remotely(world, monkeypatch, intake, bucket)
    assert outcome != scanning.ScanOutcome.VERIFIED
    assert intake.quarantine_key in bucket.objects


@pytest.mark.parametrize("raised", [ValueError, KeyError])
def test_an_unidentifiable_key_never_stops_the_batch_and_fails_closed(
    world: World, monkeypatch: pytest.MonkeyPatch, raised: type[Exception]
) -> None:
    bad, _ = _ordinary_pending(world, with_final=True)
    other, _ = _ordinary_pending(world, with_final=True)
    make_firm(f"axrm051-r14-{uuid4().hex[:8]}", client_count=1)  # a second tenant
    real_path = default_storage.path

    def refusing(name: str) -> str:
        if name == bad.storage_key:
            message = "the backend cannot address this name"
            raise raised(message)
        return real_path(name)

    decided: dict[UUID, str] = {}
    kept_as = retention_tasks._kept_as

    def recording(
        candidate: retention_tasks.Candidate, now: datetime, foreign: set[str]
    ) -> str:
        decided[candidate.id] = kept_as(candidate, now, foreign)
        return decided[candidate.id]

    monkeypatch.setattr(default_storage, "path", refusing)
    monkeypatch.setattr(retention_tasks, "_kept_as", recording)
    escaped: BaseException | None = None
    try:
        purge_tenant(
            world.tenant_id, dry_run=False, now=datetime(2030, 1, 1, tzinfo=SAO_PAULO)
        )
    except Exception as error:  # noqa: BLE001 - the escape itself is asserted
        escaped = error
    assert escaped is None
    # Both are reached and decided by their own ids, and both are kept: the bad
    # candidate's unknown identity matches every row, and its row may name the
    # neighbour's file too (fail closed both ways). Nothing is claimed.
    assert (decided.get(bad.pk), decided.get(other.pk)) == ("retained", "retained")
    assert (_claim(world, bad.pk), _claim(world, other.pk)) == (None, None)
    assert default_storage.exists(other.storage_key)


def test_cleanup_sees_a_held_owner_through_a_symlink_in_the_rows_root(
    world: World,
) -> None:
    _document, intake = _ordinary_pending(world, with_final=False)
    root = Path(default_storage.path("")).resolve()
    sub = f"pilot-{uuid4().hex[:8]}"
    nested = FileSystemStorage(location=str(root / sub))  # `/srv/media/pilot`
    with default_storage.open(intake.quarantine_key, "rb") as handle:
        nested.save(intake.quarantine_key, ContentFile(handle.read()))
    legacy = root / f"legacy-{uuid4().hex[:8]}"  # `/srv/media/legacy` -> pilot
    legacy.symlink_to(root / sub, target_is_directory=True)
    try:
        _foreign_copy(world, f"{legacy.name}/{intake.quarantine_key}")
        scanning.scan_intake(
            world.tenant_id,
            intake.pk,
            scanner=FakeScanner(),
            now=timezone.now(),
            storage=nested,
        )
        assert nested.exists(intake.quarantine_key)
    finally:
        legacy.unlink()


def test_any_storage_but_the_rows_own_is_refused(world: World) -> None:
    root = Path(default_storage.path("")).resolve()
    nested = FileSystemStorage(location=str(root / f"pilot-{uuid4().hex[:8]}"))
    key = nested.save(f"quarantine/{uuid4().hex}", ContentFile(b"copy"))
    refusal = retention.ownership_refusal(world.tenant_id, [key], storage=nested)
    assert refusal == "armazenamento_divergente"


def test_promotion_never_repoints_a_document_a_disposal_has_claimed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _document, intake = _ordinary_pending(world, with_final=False)
    old = _document_key(world, intake)
    bucket = _remote_bucket(intake, "faithful")
    real_save = bucket._save

    def claimed_meanwhile(name: str, content: File[bytes]) -> str:
        # The purge claims the document's keys while the upload is in flight.
        with tenant_context(world.tenant_id):
            ContentPurgeClaim.objects.create(
                tenant_id=world.tenant_id,
                client_id=intake.client_id,
                content_kind="document",
                content_id=intake.document_id,
                storage_keys=[old, intake.quarantine_key],
            )
        return real_save(name, content)

    monkeypatch.setattr(bucket, "_save", claimed_meanwhile)
    outcome = _promote_remotely(world, monkeypatch, intake, bucket)
    assert outcome != scanning.ScanOutcome.VERIFIED
    assert _document_key(world, intake) == old  # the claim still names the row's keys
    with tenant_context(world.tenant_id):
        claim = ContentPurgeClaim.objects.get(content_id=intake.document_id)
    assert set(claim.storage_keys) == {old, intake.quarantine_key}


def test_the_purge_reads_and_claims_a_document_under_its_row_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    document, _ = _ordinary_pending(world, with_final=True)
    read = retention_tasks._candidates
    seen: list[str] = []

    def probing(*args: object, **kwargs: object) -> object:
        target = args[0] if args else None
        ours = getattr(target, "kind", "") == "document" and (
            getattr(target, "id", None) == document.pk
        )
        if ours and not seen:
            # While the purge reads THIS document (not another candidate), try its
            # row from elsewhere.
            other = connection.get_new_connection(connection.get_connection_params())
            try:
                with other.transaction():
                    # The row as this tenant and client see it (row-level security).
                    other.execute(
                        "SELECT set_config('app.tenant_id', %s, true), "
                        "set_config('app.client_id', %s, true)",
                        [str(world.tenant_id), str(document.client_id)],
                    )
                    try:
                        visible = other.execute(
                            psycopg.sql.SQL(
                                "SELECT 1 FROM {} WHERE id = %s "
                                "FOR NO KEY UPDATE NOWAIT"
                            ).format(psycopg.sql.Identifier(Document._meta.db_table)),
                            [document.pk],
                        ).fetchall()
                        seen.append("visible-unlocked" if visible else "invisible")
                    except psycopg.errors.LockNotAvailable:
                        seen.append("locked")
                        raise
            except psycopg.errors.LockNotAvailable:
                pass  # the failed transaction is rolled back; the outcome is kept
            finally:
                other.close()
        return read(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(retention_tasks, "_candidates", probing)
    purge_tenant(
        world.tenant_id, dry_run=False, now=datetime(2030, 1, 1, tzinfo=SAO_PAULO)
    )
    assert seen == ["locked"]


@pytest.mark.parametrize(
    ("file_mode", "directory_mode"), [(0o600, 0o700), (0o664, 0o775)]
)
def test_the_exclusive_create_applies_the_configured_modes(
    world: World, settings: SettingsWrapper, file_mode: int, directory_mode: int
) -> None:
    _document, intake = _ordinary_pending(world, with_final=False)
    final = f"originals/{uuid4().hex}/final.pdf"  # a directory that does not exist
    with tenant_context(world.tenant_id):
        Document.objects.filter(pk=intake.document_id).update(storage_key=final)
    # The second pair has group write, which umask 0022 strips: only the explicit
    # modes, as `FileSystemStorage.save` applies them, give it back.
    settings.FILE_UPLOAD_PERMISSIONS = file_mode
    settings.FILE_UPLOAD_DIRECTORY_PERMISSIONS = directory_mode
    previous = os.umask(0o022)
    try:
        outcome = scanning.scan_intake(
            world.tenant_id, intake.pk, scanner=FakeScanner(), now=timezone.now()
        )
    finally:
        os.umask(previous)
    assert outcome == scanning.ScanOutcome.VERIFIED
    path = Path(default_storage.path(final))
    assert stat.S_IMODE(path.stat().st_mode) == file_mode
    assert stat.S_IMODE(path.parent.stat().st_mode) == directory_mode


def test_a_candidate_whose_identity_is_unknown_matches_every_row(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _document, intake = _ordinary_pending(world, with_final=False)
    key = intake.quarantine_key
    _foreign_copy(world, key.replace("/", "/./", 1))  # a held row, spelled another way
    known = retention._identity_or_none

    def unknown_for_the_candidate(name: str, storage: Storage) -> bytes | None:
        return None if name == key else known(name, storage)

    monkeypatch.setattr(retention, "_identity_or_none", unknown_for_the_candidate)
    assert retention.foreign_storage_keys(world.tenant_id, [key], default_storage) == {
        key
    }


def test_a_bind_mounted_view_of_a_held_file_is_the_same_file(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    document, _intake = _ordinary_pending(world, with_final=True)
    root = Path(default_storage.path("")).resolve()
    final = root / document.storage_key
    view = root / "view" / Path(document.storage_key).name
    # Stand-in for a bind-mounted view (which needs privileges): at the lstat
    # boundary, the view's path is the final's own inode.
    real_lstat = retention._lstat
    held = final.lstat()

    def through_the_view(path: object) -> os.stat_result:
        return held if Path(os.fsdecode(path)) == view else real_lstat(path)  # type: ignore[arg-type]

    monkeypatch.setattr(retention, "_lstat", through_the_view, raising=False)
    _foreign_original(world, f"view/{Path(document.storage_key).name}")
    purge_tenant(
        world.tenant_id, dry_run=False, now=datetime(2030, 1, 1, tzinfo=SAO_PAULO)
    )
    assert final.exists()
    assert _claim(world, document.pk) is None


def test_promotion_verifies_the_file_the_row_names(world: World) -> None:
    document, intake = _ordinary_pending(world, with_final=False)
    root = Path(default_storage.path("")).resolve()
    nested = FileSystemStorage(location=str(root / f"pilot-{uuid4().hex[:8]}"))
    with default_storage.open(intake.quarantine_key, "rb") as handle:
        nested.save(intake.quarantine_key, ContentFile(handle.read()))
    # A different benign PDF already serves the row's key in the rows' storage.
    other_pdf = b"%PDF-1.7\n% another benign document\n%%EOF\n"
    default_storage.save(document.storage_key, ContentFile(other_pdf))
    outcome = scanning.scan_intake(
        world.tenant_id,
        intake.pk,
        scanner=FakeScanner(),
        now=timezone.now(),
        storage=nested,
    )
    assert outcome != scanning.ScanOutcome.VERIFIED
    with default_storage.open(document.storage_key, "rb") as handle:
        assert handle.read() == other_pdf


@pytest.mark.parametrize("change", ["ticket-went-stale", "key-moved"])
def test_a_remote_repoint_rechecks_the_ticket(
    world: World, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    _document, intake = _ordinary_pending(world, with_final=False)
    old = _document_key(world, intake)
    moved = f"originals/{uuid4().hex}.pdf"
    bucket = _remote_bucket(intake, "faithful")
    real_save = bucket._save

    def meanwhile(name: str, content: File[bytes]) -> str:
        # Another attempt acts while this upload is in flight.
        with tenant_context(world.tenant_id):
            if change == "ticket-went-stale":
                DocumentIntake.objects.filter(pk=intake.pk).update(
                    version=F("version") + 1
                )
            else:
                Document.objects.filter(pk=intake.document_id).update(storage_key=moved)
        return real_save(name, content)

    monkeypatch.setattr(bucket, "_save", meanwhile)
    outcome = _promote_remotely(world, monkeypatch, intake, bucket)
    # A lost race, never a digest failure: deferred, and the row left as it was.
    assert outcome == scanning.ScanOutcome.SKIPPED
    assert _document_key(world, intake) == (
        old if change == "ticket-went-stale" else moved
    )


def test_the_final_is_never_wider_than_configured_even_before_its_chmod(
    world: World, settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    document, intake = _ordinary_pending(world, with_final=False)
    final = Path(default_storage.path(document.storage_key))
    final.parent.mkdir(parents=True, exist_ok=True)
    final.parent.chmod(0o755)  # an existing, traversable directory
    settings.FILE_UPLOAD_PERMISSIONS = 0o600
    real_chmod = Path.chmod

    def stopped(self: Path, mode: int, *args: object, **kwargs: object) -> None:
        if self == final:
            message = "the worker stops before its chmod"
            raise OSError(message)
        real_chmod(self, mode, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "chmod", stopped)
    previous = os.umask(0o022)
    try:
        scanning.scan_intake(
            world.tenant_id, intake.pk, scanner=FakeScanner(), now=timezone.now()
        )
    finally:
        os.umask(previous)
    assert stat.S_IMODE(final.stat().st_mode) == 0o600


def test_the_layout_proof_refuses_a_key_whose_identity_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = f"originals/{uuid4().hex}.pdf"
    real_path = default_storage.path

    def refusing(name: str) -> str:
        if name == key:
            message = "the backend cannot address this name"
            raise KeyError(message)
        return real_path(name)

    monkeypatch.setattr(default_storage, "path", refusing)
    try:
        refusal = retention.local_layout_refusal([key], default_storage)
    except Exception as error:  # noqa: BLE001 - an escape is itself the failure
        refusal = f"raised {error!r}"
    assert refusal == "identidade_indeterminada"


def test_a_failed_batch_lookup_is_redone_per_candidate(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    document, _ = _ordinary_pending(world, with_final=True)
    lookup = retention.foreign_storage_keys
    calls: list[int] = []

    def failing_once(*args: object, **kwargs: object) -> set[str]:
        calls.append(1)
        if len(calls) == 1:
            message = "the batch lookup failed"
            raise RuntimeError(message)
        return lookup(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(retention_tasks, "foreign_storage_keys", failing_once)
    escaped: BaseException | None = None
    try:
        purge_tenant(
            world.tenant_id, dry_run=False, now=datetime(2030, 1, 1, tzinfo=SAO_PAULO)
        )
    except Exception as error:  # noqa: BLE001 - the escape itself is asserted
        escaped = error
    assert escaped is None
    claim = _claim(world, document.pk)
    assert claim is not None  # reached, decided and disposed of on its own lookup
    assert claim.completed_at is not None


def test_cleanup_through_another_storage_object_keeps_the_copy(world: World) -> None:
    _document, intake = _ordinary_pending(world, with_final=False)
    # The same root, through a second storage object: never the rows' own.
    same_root = FileSystemStorage(
        location=str(Path(default_storage.path("")).resolve())
    )
    outcome = scanning.scan_intake(
        world.tenant_id,
        intake.pk,
        scanner=FakeScanner(),
        now=timezone.now(),
        storage=same_root,
    )
    assert outcome == scanning.ScanOutcome.VERIFIED
    assert default_storage.exists(intake.quarantine_key)


@pytest.mark.parametrize("spelling", ["leading-slash", "leading-dotdot"])
def test_a_remote_key_is_the_object_the_backend_resolves(
    world: World, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    # The production backend itself, as configured there (no location); its own
    # name resolution is all that is exercised, so no request is ever sent.
    bucket = S3Storage(
        bucket_name="axrm051-synthetic",
        endpoint_url="https://storage.invalid",
        access_key="synthetic-access",
        secret_key="synthetic-secret",  # noqa: S106 - no request is ever sent
    )
    monkeypatch.setattr(retention, "rows_storage", lambda: bucket, raising=False)
    key = f"originals/{uuid4().hex}.pdf"
    held = f"/{key}" if spelling == "leading-slash" else f"../{key}"
    # One object to S3 (the stubs do not declare its name resolution).
    assert bucket._normalize_name(clean_name(held)) == key  # type: ignore[attr-defined]
    _foreign_original(world, held)  # another tenant's held original, so spelled
    refusal = retention.ownership_refusal(world.tenant_id, [key], storage=bucket)
    assert refusal == "chave_compartilhada"


def test_a_lock_name_that_cannot_be_computed_keeps_the_file(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    document, _ = _ordinary_pending(world, with_final=True)
    named = retention._path_identity
    failed: list[str] = []

    def fails_once(key: str, storage: Storage) -> bytes:
        if key == document.storage_key and not failed:
            failed.append(key)  # the backend fails once, as the lock is named
            message = "the backend failed once"
            raise ValueError(message)
        return named(key, storage)

    monkeypatch.setattr(retention, "_path_identity", fails_once, raising=False)
    purge_tenant(
        world.tenant_id, dry_run=False, now=datetime(2030, 1, 1, tzinfo=SAO_PAULO)
    )
    assert failed == [document.storage_key]
    assert default_storage.exists(document.storage_key)
    claim = _claim(world, document.pk)
    assert claim is not None
    assert claim.completed_at is None


def _decided(world: World, document_id: UUID) -> str:
    """Return the pre-claim refusal the sweep gives this document."""
    with tenant_context(world.tenant_id):
        (candidate,) = [c for c in retention_tasks._candidates() if c.id == document_id]
        return retention_tasks.disposal_refusal(candidate)


@pytest.mark.parametrize("via", ["intake-copy", "document-final"])
@pytest.mark.parametrize("held", ["provider-journal", "account-export", "tenant-logo"])
def test_a_restored_intake_never_reaches_another_kinds_object(
    world: World, held: str, via: str
) -> None:
    document, _intake = _ordinary_pending(world, with_final=True)
    other = uuid4()  # another tenant; no row anywhere names its object
    key = {
        "provider-journal": f"provider-journal/{other}/2026/10/{uuid4()}-1-result.json",
        "account-export": f"account-exports/{other}/{uuid4()}/0-x/archive.zip",
        "tenant-logo": f"branding/{other}/{uuid4()}/logo.png",
    }[held]
    saved = default_storage.save(key, ContentFile(b"another tenant's object"))
    with tenant_context(world.tenant_id):
        # A restored descriptor: the intake's copy, or the document's own final.
        if via == "intake-copy":
            DocumentIntake.objects.filter(document_id=document.pk).update(
                quarantine_key=saved
            )
        else:
            Document.objects.filter(pk=document.pk).update(storage_key=saved)
    assert _decided(world, document.pk) == "chave_fora_do_prefixo"
    purge_tenant(
        world.tenant_id, dry_run=False, now=datetime(2030, 1, 1, tzinfo=SAO_PAULO)
    )
    assert default_storage.exists(saved)
    assert _claim(world, document.pk) is None


def test_a_dot_dot_escape_from_the_copies_prefix_is_refused(world: World) -> None:
    document, intake = _ordinary_pending(world, with_final=True)
    prefix = posixpath.dirname(intake.quarantine_key)
    target = default_storage.save(
        f"provider-journal/{uuid4()}/2026/10/{uuid4()}-1-result.json",
        ContentFile(b"another tenant's object"),
    )
    escaped = f"{prefix}/../../../{target}"  # starts with the prefix as spelled
    with tenant_context(world.tenant_id):
        DocumentIntake.objects.filter(pk=intake.pk).update(quarantine_key=escaped)
    assert _decided(world, document.pk) == "chave_fora_do_prefixo"
    purge_tenant(
        world.tenant_id, dry_run=False, now=datetime(2030, 1, 1, tzinfo=SAO_PAULO)
    )
    assert default_storage.exists(target)


def test_a_dot_dot_escape_is_refused_on_the_name_s3_resolves(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    document, intake = _ordinary_pending(world, with_final=True)
    bucket = S3Storage(
        bucket_name="axrm051-synthetic",
        endpoint_url="https://storage.invalid",
        access_key="synthetic-access",
        secret_key="synthetic-secret",  # noqa: S106 - no request is ever sent
    )
    # One default storage, as in production: S3, never contacted here.
    monkeypatch.setattr(retention, "default_storage", bucket)
    monkeypatch.setattr(retention_tasks, "default_storage", bucket)
    prefix = posixpath.dirname(intake.quarantine_key)
    escaped = f"{prefix}/../../../provider-journal/{uuid4()}/2026/10/r-result.json"
    assert not bucket._normalize_name(clean_name(escaped)).startswith(  # type: ignore[attr-defined]
        "quarantine/"
    )
    with tenant_context(world.tenant_id):
        candidate = retention_tasks.Candidate(
            document.client_id,
            "document",
            document.pk,
            None,
            (document.storage_key, escaped),
        )
        assert retention_tasks.disposal_refusal(candidate) == "chave_fora_do_prefixo"


def test_an_export_archive_outside_its_own_prefix_is_refused(world: World) -> None:
    with tenant_context(world.tenant_id):
        archive = retention_tasks.Candidate(
            world.termo.client.pk,
            "export",
            uuid4(),
            None,
            (f"account-exports/{world.tenant_id}/{uuid4()}/0-x/archive.zip",),
        )
        assert retention_tasks.disposal_refusal(archive) == "chave_fora_do_prefixo"


@pytest.mark.parametrize("offered_as", ["copy", "final"])
def test_cleanup_never_deletes_through_an_object_of_another_kind(
    world: World, offered_as: str
) -> None:
    _document, intake = _ordinary_pending(world, with_final=False)
    with default_storage.open(intake.quarantine_key, "rb") as handle:
        payload = handle.read()
    # Another kind's object holding the original's exact bytes, so every digest
    # check passes: only the confinement to cleanup's own names can refuse.
    foreign = default_storage.save(
        f"provider-journal/{uuid4()}/2026/10/{uuid4()}-1-result.json",
        ContentFile(payload),
    )
    with tenant_context(world.tenant_id):
        if offered_as == "copy":
            DocumentIntake.objects.filter(pk=intake.pk).update(quarantine_key=foreign)
        else:
            Document.objects.filter(pk=intake.document_id).update(storage_key=foreign)
    scanning.scan_intake(
        world.tenant_id, intake.pk, scanner=FakeScanner(), now=timezone.now()
    )
    assert default_storage.exists(foreign)
    if offered_as == "final":
        # The intake's own copy is the only copy cleanup could have deleted.
        assert default_storage.exists(intake.quarantine_key)


def test_cleanup_sees_two_spellings_of_one_file_as_one_object() -> None:
    key = default_storage.save(f"originals/{uuid4().hex}.pdf", ContentFile(b"one"))
    other = default_storage.save(f"originals/{uuid4().hex}.pdf", ContentFile(b"one"))
    doubled = key.replace("/", "//", 1)
    assert scanning._may_be_one_object(default_storage, doubled, key) is True
    assert scanning._may_be_one_object(default_storage, other, key) is False
