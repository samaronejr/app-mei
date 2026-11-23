"""A SITFIS original is disposed of only when that is provably safe (AXRM-051 B-1).

The purge job refuses, before a claim exists and before an open claim deletes
anything, every case it cannot prove: an unresolved or missing date, a scan still in
flight, storage that keeps versions a hold cannot protect. The one supported case,
local single-copy storage, is witnessed end to end: an interrupted claim resumes once
and keeps the receipts and the barrier against a second emission.
"""

from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.utils import timezone
from storages.backends.s3 import S3Storage

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles import retention_tasks
from apps.cycles.errors import IllegalTransition
from apps.cycles.models import ServiceCycle
from apps.cycles.retention import place_hold
from apps.cycles.retention_records import ContentPurgeClaim
from apps.cycles.retention_tasks import purge_tenant
from apps.cycles.services.federal_review import (
    record_federal_report_review,
    report_input_digest,
)
from apps.integrations.call_models import ProviderCall
from apps.integrations.serpro import retention as serpro_retention
from apps.obligations.models import (
    AcquisitionOrigin,
    Document,
    FederalConsultation,
    FiscalSituationReport,
)
from apps.obligations.system_ingest import ingest_system_document
from tests.integrations.sitfis_support import (
    World,
    clock,
    protocol_reply,
    report_reply,
    world,
)

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "world"]

SAO_PAULO = ZoneInfo("America/Sao_Paulo")


def _emitted(world: World, *, scan: bool = True) -> FiscalSituationReport:
    world.script(protocol_reply(30_000))
    report = world.advance(world.request())
    world.clock.advance(seconds=30)
    world.script(report_reply())
    emitted = world.advance(report)
    assert emitted.state == "emitido"
    if scan:
        world.scan()
    return world.reload(emitted)


def _document(world: World, report: FiscalSituationReport) -> Document:
    assert report.document_id is not None
    with tenant_context(world.tenant_id):
        return Document.objects.get(pk=report.document_id)


def _expiry(report: FiscalSituationReport) -> datetime:
    assert report.emitted_at is not None
    year = timezone.localdate(report.emitted_at).year
    return datetime(year + 6, 1, 1, tzinfo=SAO_PAULO)


def _claimed(world: World, document_id: UUID | None) -> bool:
    assert document_id is not None
    with tenant_context(world.tenant_id):
        return ContentPurgeClaim.objects.filter(content_id=document_id).exists()


def _client(world: World) -> ClientCompany:
    with tenant_context(world.tenant_id):
        return ClientCompany.objects.get(pk=world.termo.client.pk)


def test_an_interrupted_claim_resumes_once_and_keeps_receipts_and_barrier(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _emitted(world)
    document = _document(world, report)
    expiry = _expiry(report)
    with tenant_context(world.tenant_id):
        kept = FiscalSituationReport.objects.values(
            "state", "document_id", "emitted_at", "call_replies", "consultation_id"
        ).get(pk=report.pk)
        consultations = set(FederalConsultation.objects.values_list("pk", "outcome"))
        calls = set(ProviderCall.objects.values_list("pk", "state", "billed"))
    requests = len(world.gateway.requests)
    delete = default_storage.delete

    def refuse(key: str) -> None:
        message = "synthetic storage refusal"
        raise OSError(message)

    # Nothing is claimed one second early.
    purge_tenant(world.tenant_id, dry_run=False, now=expiry - timedelta(seconds=1))
    assert not _claimed(world, document.pk)
    # The claim commits, then storage refuses the delete.
    monkeypatch.setattr(default_storage, "delete", refuse)
    assert purge_tenant(world.tenant_id, dry_run=False, now=expiry)["failed"] == 1
    assert default_storage.exists(document.storage_key)
    client = _client(world)
    with tenant_context(world.tenant_id), pytest.raises(IllegalTransition):
        place_hold(
            world.termo.actor,
            client.pk,
            reason="Depois da reivindicação",
            expected_version=client.version,
        )
    # The next run resumes the same claim and completes it once.
    monkeypatch.setattr(default_storage, "delete", delete)
    assert purge_tenant(world.tenant_id, dry_run=False, now=expiry)["purged"] == 1
    assert purge_tenant(world.tenant_id, dry_run=False, now=expiry)["purged"] == 0
    assert not default_storage.exists(document.storage_key)
    with tenant_context(world.tenant_id):
        [claim] = ContentPurgeClaim.objects.filter(content_id=document.pk)
        assert (claim.completed_at, claim.error_code) == (expiry, "")
    # An overlapping run that reaches the finish later completes nothing.
    assert not retention_tasks._finish(world.tenant_id, client.pk, claim.pk, expiry)
    with tenant_context(world.tenant_id):
        purged = Document.objects.get(pk=document.pk)
        assert (purged.sha256, purged.acquisition_receipt_ref, purged.purged_at) == (
            document.sha256,
            document.acquisition_receipt_ref,
            expiry,
        )
        assert (
            FiscalSituationReport.objects.values(
                "state", "document_id", "emitted_at", "call_replies", "consultation_id"
            ).get(pk=report.pk)
            == kept
        )
        assert set(FederalConsultation.objects.values_list("pk", "outcome")) == (
            consultations
        )
        assert set(ProviderCall.objects.values_list("pk", "state", "billed")) == calls
    # The emitted report is the barrier: advancing it sends nothing again.
    assert world.advance(report).state == "emitido"
    assert len(world.gateway.requests) == requests


def test_a_reviewed_report_keeps_its_emission_date_as_a_reference(
    world: World,
) -> None:
    report = _emitted(world)
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
        anchors, gap = retention_tasks._document_anchors(_document(world, report))
    assert gap == ""
    assert {(anchor.basis, anchor.source_id) for anchor in anchors} == {
        ("revisao_ciclo", cycle.pk),
        ("emissao_relatorio", report.pk),
    }


def test_a_request_date_never_dates_a_report_without_its_emission(
    world: World,
) -> None:
    report = _emitted(world)
    with tenant_context(world.tenant_id):
        current = FiscalSituationReport.objects.get(pk=report.pk)
    # A legacy shape: the PDF and a December 31 request, no emission date.
    current.requested_at = datetime(2020, 12, 31, 12, tzinfo=SAO_PAULO)
    current.emitted_at = None
    with tenant_context(world.tenant_id):
        assert serpro_retention._report_anchors(current) == ([], ["emissao_ausente"])


def test_a_staff_original_a_report_holds_is_dated_by_the_report(
    world: World,
) -> None:
    report = _emitted(world)
    document = _document(world, report)
    # The staff upload intake deduplicated into: same bytes, staff origin.
    document.acquisition_origin = AcquisitionOrigin.UPLOAD_STAFF
    with tenant_context(world.tenant_id):
        anchors, gap = retention_tasks._document_anchors(document)
    assert (gap, [(a.basis, a.source_id) for a in anchors]) == (
        "",
        [("emissao_relatorio", report.pk)],
    )


def test_a_hold_committed_before_the_claim_keeps_the_original(world: World) -> None:
    report = _emitted(world)
    client = _client(world)
    with tenant_context(world.tenant_id):
        place_hold(
            world.termo.actor,
            client.pk,
            reason="Fiscalização em andamento",
            expected_version=client.version,
        )
    counts = purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))
    assert counts["held"] >= 1
    assert (counts["eligible"], counts["purged"]) == (0, 0)
    assert not _claimed(world, report.document_id)


def test_a_review_is_refused_while_its_original_has_an_open_claim(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _emitted(world)

    def refuse(key: str) -> None:
        message = "synthetic storage refusal"
        raise OSError(message)

    monkeypatch.setattr(default_storage, "delete", refuse)
    purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))
    assert _claimed(world, report.document_id)
    with tenant_context(world.tenant_id):
        cycle = ServiceCycle.objects.get(pk=world.cycle.pk)
        current = FiscalSituationReport.objects.select_related("document").get(
            pk=report.pk
        )
        with pytest.raises(ValidationError):
            record_federal_report_review(
                world.responsible,
                cycle.pk,
                report.pk,
                outcome="sem_pendencias_nas_fontes",
                pendencias=[],
                expected_version=cycle.version,
                input_digest=report_input_digest(current),
            )


def test_unproved_cases_are_kept_and_the_run_continues(world: World) -> None:
    report = _emitted(world)
    with tenant_context(world.tenant_id):
        call_id = ProviderCall.objects.get(operation="SP-SIT-R").pk
        sibling = ClientCompany.objects.create(
            tenant=world.termo.tenant, cnpj="11444777000161", legal_name="Sibling"
        )
    originals = {
        reason: ingest_system_document(
            tenant_id=world.tenant_id,
            client_id=sibling.pk,
            origin=AcquisitionOrigin.FEDERAL_PROVIDER,
            receipt_ref=receipt,
            payload=f"%PDF-1.7\n% synthetic {reason} AXRM-051\n%%EOF\n".encode(),
            filename=f"{reason}.pdf",
            content_type="application/pdf",
            retention_class="fiscal_evidence",
        )
        for reason, receipt in (
            ("recibo_invalido", "sitfis-call-001"),
            ("chamada_ausente", str(call_id)),
        )
    }
    with tenant_context(world.tenant_id):
        for reason, original in originals.items():
            assert retention_tasks._document_anchors(original)[1] == reason
    far = _expiry(report) + timedelta(days=3650)
    purge_tenant(world.tenant_id, dry_run=False, now=far)
    for original in originals.values():
        assert not _claimed(world, original.pk)
    # The report's own original, with a resolved date, was still disposed of.
    assert _claimed(world, report.document_id)


def test_a_scan_in_flight_keeps_the_original(world: World) -> None:
    report = _emitted(world, scan=False)
    purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))
    assert not _claimed(world, report.document_id)


@pytest.mark.parametrize("held", [False, True])
def test_versioned_storage_is_unsupported_for_federal_disposal(
    world: World, monkeypatch: pytest.MonkeyPatch, held: bool
) -> None:
    report = _emitted(world)
    if held:
        client = _client(world)
        with tenant_context(world.tenant_id):
            place_hold(
                world.termo.actor,
                client.pk,
                reason="Fiscalização em andamento",
                expected_version=client.version,
            )
    bucket = S3Storage(
        bucket_name="axrm051-synthetic",
        endpoint_url="https://storage.invalid",
        access_key="synthetic-access",
        secret_key="synthetic-secret",  # noqa: S106 - no request is ever sent
    )
    monkeypatch.setattr(retention_tasks, "default_storage", bucket)
    with tenant_context(world.tenant_id):
        candidate = next(
            c for c in retention_tasks._candidates() if c.id == report.document_id
        )
        assert retention_tasks.disposal_refusal(candidate) == (
            "armazenamento_nao_suportado"
        )
    counts = purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))
    assert counts["purged"] == 0
    assert not _claimed(world, report.document_id)
