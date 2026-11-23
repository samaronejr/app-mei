"""A federal copy is disposed of only when its disposal is proved (AXRM-051 B-1).

These tests drive the real purge, scanner and writer boundaries with a SITFIS report:
a claim completes only when the stored bytes were the original's single copy and
are gone afterwards; storage support is an allow-list; the scanner never writes a
disposed original back nor drops a held federal copy; and writers that would add a
use refuse once a disposal claim exists, also across tenants.
"""

import hashlib
from datetime import date, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, Storage, default_storage
from django.utils import timezone
from storages.backends.s3 import S3Storage

from apps.audit.models import Event
from apps.core.tenancy import tenant_context
from apps.cycles import retention, retention_tasks, scanning
from apps.cycles.evidence import CLEAN_STATES, DocumentIntake, IntakeState, ScanVerdict
from apps.cycles.models import ServiceCycle
from apps.cycles.retention import place_hold, release_hold
from apps.cycles.retention_records import ContentPurgeClaim
from apps.cycles.retention_tasks import Candidate, disposal_refusal, purge_tenant
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


@pytest.mark.parametrize("window", ["before_write", "after_write"])
def test_a_stale_scanner_never_writes_a_disposed_original_back(
    world: World, monkeypatch: pytest.MonkeyPatch, window: str
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
    if window == "after_write":
        # A passed its check just before B finished: only the second check is left.
        real = scanning._write_current
        seen: list[object] = []

        def passed_once(ticket: scanning.ScanTicket) -> bool:
            seen.append(ticket)
            return True if len(seen) == 1 else real(ticket)

        monkeypatch.setattr(scanning, "_write_current", passed_once)
    default_storage.save(stale.object_key, ContentFile(PDF))
    saved: list[str] = []
    save = default_storage.save

    def recorded(name: str, content: ContentFile[bytes]) -> str:
        saved.append(name)
        return save(name, content)

    monkeypatch.setattr(default_storage, "save", recorded)
    scanning.promote(stale, default_storage)
    assert not default_storage.exists(original.storage_key)
    # Before the write the stale ticket writes nothing at all.
    assert saved == ([] if window == "before_write" else [original.storage_key])


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
        candidate = Candidate(world.termo.client.pk, "document", original.pk, None)
        assert disposal_refusal(candidate) == "chave_compartilhada"
    purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))
    assert _claim(world, original.pk) is None
    assert default_storage.exists(original.storage_key)


def test_a_leftover_copy_or_an_undone_delete_never_completes_a_claim(
    world: World, monkeypatch: pytest.MonkeyPatch
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
    world.scan()  # the held client's quarantine copy is kept
    with tenant_context(world.tenant_id):
        release_hold(world.termo.actor, hold.pk, expected_version=hold.version)
    purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))
    claim = _claim(world, report.document_id)
    assert claim is not None
    assert (claim.completed_at, claim.error_code) == (None, "copias_multiplas")


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
