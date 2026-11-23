"""A federal copy is disposed of only when its disposal is proved (AXRM-051 B-1).

These tests drive the real purge, scanner and writer boundaries with a SITFIS report:
a claim completes only when the stored bytes were the original's single copy and
are gone afterwards; storage support is an allow-list; the scanner never writes a
disposed original back nor drops a held federal copy; and writers that would add a
use refuse once a disposal claim exists, also across tenants.
"""

import hashlib
import threading
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, Storage, default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.transaction import TransactionManagementError
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from storages.backends.s3 import S3Storage

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
from apps.cycles.models import ServiceCycle
from apps.cycles.retention import place_hold, release_hold
from apps.cycles.retention_records import ContentPurgeClaim
from apps.cycles.retention_tasks import Candidate, disposal_refusal, purge_tenant
from apps.cycles.scanning import FakeScanner
from apps.cycles.services.federal_review import (
    record_federal_report_review,
    report_input_digest,
)
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
    save = default_storage.save
    interleaved: list[str] = []

    def save_then_let_the_other_scanner_finish(
        name: str, content: ContentFile[bytes]
    ) -> str:
        stored = save(name, content)
        if not interleaved:
            interleaved.append(stored)
            monkeypatch.setattr(default_storage, "save", save)
            world.scan()  # scanner B verifies A's copy and removes the quarantine
        return stored

    monkeypatch.setattr(default_storage, "save", save_then_let_the_other_scanner_finish)
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
    world: World,
) -> None:
    report = _emit(world)
    with tenant_context(world.tenant_id):
        intake = DocumentIntake.objects.get(document=_original(world, report))
    ticket = scanning.claim_scan(world.tenant_id, intake.pk)
    assert ticket is not None
    bucket = RecordingBucket()
    scanning._discard_quarantine(ticket, bucket)
    assert bucket.calls == []


def _ordinary_pending(
    world: World, *, with_final: bool
) -> tuple[Document, DocumentIntake]:
    """An expired ordinary original (2018) with a pending intake and its copy."""
    termo = world.termo
    payload = b"%PDF-1.7\n% ordinary original AXRM-051 r5\n%%EOF\n"
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
