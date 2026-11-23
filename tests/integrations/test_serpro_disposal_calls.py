"""Guides and receipts are disposed of only on a resolved, complete date (AXRM-051 B-1).

A DAS guide is dated by the competence of the cycle its request was recorded for, a
PAGTOWEB receipt by the collection date of the payment it was asked for. Each lookup
stays inside the original's client. Identical bytes reused by a second acquisition,
a collection date outside its own consultation window, a request of another client
and authority evidence all keep the original before any claim exists.
"""

import base64
import hashlib
import json
from datetime import date, datetime, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from storages.backends.s3 import S3Storage

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles import retention, retention_tasks
from apps.cycles.errors import IllegalTransition
from apps.cycles.evidence import DocumentIntake, IntakeState, ScanVerdict
from apps.cycles.intake import UploadRejected
from apps.cycles.retention_records import ContentPurgeClaim
from apps.cycles.retention_tasks import Candidate, disposal_refusal, purge_tenant
from apps.cycles.scanning import FakeScanner
from apps.cycles.tasks import scan_quarantine
from apps.integrations.authority import declare_authority, head_record
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.models import FirmAuthorization, ProviderConnection
from apps.integrations.serpro import authorization, pgmei
from apps.integrations.serpro import retention as serpro_retention
from apps.integrations.serpro.authorization import TermoSubmission
from apps.integrations.serpro.payments import emit_payment_receipt
from apps.integrations.serpro.termo import TermoInvalidoError
from apps.obligations.models import (
    AcquisitionOrigin,
    Document,
    FederalPayment,
    Obligation,
    ObligationType,
)
from apps.obligations.system_ingest import ingest_system_document
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations.pagtoweb_support import PDF, World, page, receipt_body, world
from tests.integrations.pagtoweb_support import item as arrecadacao
from tests.integrations.pgmei_support import (
    GUIDE_PDF,
    GuideWorld,
    das_reply,
    guide_world,
)

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["guide_world", "world"]

SAO_PAULO = ZoneInfo("America/Sao_Paulo")


def _expiry(year: int) -> datetime:
    return datetime(year + 6, 1, 1, tzinfo=SAO_PAULO)


def _claimed(tenant_id: UUID, document_id: UUID) -> bool:
    with tenant_context(tenant_id):
        return ContentPurgeClaim.objects.filter(content_id=document_id).exists()


def _scan() -> None:
    with patch("apps.cycles.tasks.scanner_from_settings", return_value=FakeScanner()):
        scan_quarantine.apply().get()


def _sibling(tenant_id: UUID) -> ClientCompany:
    with tenant_context(tenant_id):
        client = ClientCompany.objects.first()
        assert client is not None
        return ClientCompany.objects.create(
            tenant=client.tenant, cnpj="11444777000161", legal_name="Sibling"
        )


def _payments(world: World, *items: dict[str, object]) -> list[FederalPayment]:
    world.script(page(list(items)))
    world.consult()
    with tenant_context(world.termo.tenant.pk):
        return list(FederalPayment.objects.order_by("numero_documento"))


def _receipt(world: World, payment: FederalPayment, pdf: bytes = PDF) -> ProviderCall:
    encoded = json.dumps({"pdf": base64.b64encode(pdf).decode()})
    world.script(Reply(body=receipt_body(encoded)))
    call = emit_payment_receipt(world.termo.tenant.pk, payment.pk, world.termo.actor.pk)
    assert call is not None
    assert call.state == "sucesso"
    return call


def _original(tenant_id: UUID, call: ProviderCall) -> Document:
    with tenant_context(tenant_id):
        return Document.objects.get(acquisition_receipt_ref=str(call.pk))


def _collected(day: str) -> dict[str, object]:
    entry = arrecadacao(f"DAS0000000{day.replace('-', '')}")
    entry["dataArrecadacao"] = f"{day}T00:00:00-03:00"
    return entry


def test_a_receipt_is_dated_by_its_payment_and_disposed_of_at_expiry(
    world: World,
) -> None:
    tenant_id = world.termo.tenant.pk
    [payment] = _payments(world, _collected("2026-10-20"))
    document = _original(tenant_id, _receipt(world, payment))
    _scan()
    with tenant_context(tenant_id):
        anchors, gap = retention_tasks._document_anchors(document)
    assert gap == ""
    assert [(a.basis, a.source_id) for a in anchors] == [
        ("arrecadacao_pagamento", payment.pk)
    ]
    expiry = _expiry(payment.data_arrecadacao.year)
    purge_tenant(tenant_id, dry_run=False, now=expiry - timedelta(seconds=1))
    assert not _claimed(tenant_id, document.pk)
    assert purge_tenant(tenant_id, dry_run=False, now=expiry)["purged"] == 1
    assert not default_storage.exists(document.storage_key)
    with tenant_context(tenant_id):
        assert FederalPayment.objects.filter(pk=payment.pk).exists()


def test_identical_receipt_bytes_for_two_payments_keep_the_original(
    world: World,
) -> None:
    tenant_id = world.termo.tenant.pk
    first, second = _payments(world, _collected("2026-09-15"), _collected("2026-10-20"))
    document = _original(tenant_id, _receipt(world, first))
    reused = _receipt(world, second)
    _scan()
    with tenant_context(tenant_id):
        # Intake kept one original for both calls; the second has none of its own.
        assert not Document.objects.filter(
            acquisition_receipt_ref=str(reused.pk)
        ).exists()
        anchors, gap = retention_tasks._document_anchors(document)
    assert gap == "proveniencia_incompleta"
    # The date that is known is still shown.
    assert [(a.basis, a.source_id) for a in anchors] == [
        ("arrecadacao_pagamento", first.pk)
    ]
    purge_tenant(tenant_id, dry_run=False, now=_expiry(2040))
    assert not _claimed(tenant_id, document.pk)
    assert default_storage.exists(document.storage_key)


def test_a_collection_date_outside_its_window_never_dates_a_receipt(
    world: World,
) -> None:
    tenant_id = world.termo.tenant.pk
    [payment] = _payments(world, _collected("2018-03-10"))
    document = _original(tenant_id, _receipt(world, payment))
    _scan()
    with tenant_context(tenant_id):
        anchors, gap = retention_tasks._document_anchors(document)
    assert (anchors, gap) == ((), "data_contraditoria")
    purge_tenant(tenant_id, dry_run=False, now=_expiry(2026))
    assert not _claimed(tenant_id, document.pk)
    assert default_storage.exists(document.storage_key)


def test_a_receipt_call_of_another_client_never_dates_a_payment(world: World) -> None:
    tenant_id = world.termo.tenant.pk
    [payment] = _payments(world, _collected("2026-10-20"))
    call = _receipt(world, payment)
    sibling = _sibling(tenant_id)
    stranger = ProviderCall(
        client_id=sibling.pk, operation=call.operation, request_key=call.request_key
    )
    with tenant_context(tenant_id):
        own = serpro_retention._receipt_anchors(call)
        foreign = serpro_retention._receipt_anchors(stranger)
    assert [a.source_id for a in own[0]] == [payment.pk]
    assert foreign == ([], ["pagamento_ausente"])


@pytest.mark.parametrize("use", ["authority", "termo"])
def test_authority_evidence_is_never_ordinary_disposal(world: World, use: str) -> None:
    termo = world.termo
    with tenant_context(termo.tenant.pk):
        document = Document.objects.create(
            tenant=termo.tenant,
            client=termo.client,
            original_filename="procuracao.pdf",
            content_type="application/pdf",
            byte_size=10,
            sha256="d" * 64,
            uploaded_by=termo.actor,
            acquisition_origin=AcquisitionOrigin.UPLOAD_STAFF,
            portal_visible=False,
        )
        candidate = Candidate(termo.client.pk, "document", document.pk, None)
        assert disposal_refusal(candidate) == ""
        if use == "authority":
            head = head_record(termo.client.pk, "sitfis")
            declare_authority(
                termo.actor,
                termo.client.pk,
                service="sitfis",
                code="00002",
                valid_from=timezone.localdate(),
                expected_version=head.version if head else 0,
                evidence_document=document,
            )
        else:
            FirmAuthorization.objects.create(
                tenant=termo.tenant,
                kind="termo_autorizacao_serpro",
                signed_document=document,
                submitted_at=timezone.now(),
                status="enviado",
            )
        assert disposal_refusal(candidate) == "evidencia_de_autorizacao"


def _obtain(world: GuideWorld) -> tuple[ProviderCall, Document]:
    with FakeSerpro((das_reply(),)) as peer:
        outcome = pgmei.obtain_das_guide(
            world.owner, world.tenant_id, world.cycle.pk, origin=peer.origin
        )
    assert outcome.document_id is not None
    with tenant_context(world.tenant_id):
        return (
            ProviderCall.objects.get(pk=outcome.call_id),
            Document.objects.get(pk=outcome.document_id),
        )


def test_a_guide_request_of_another_client_never_dates_a_guide(
    guide_world: GuideWorld,
) -> None:
    call, _document = _obtain(guide_world)
    sibling = _sibling(guide_world.tenant_id)
    stranger = ProviderCall(
        client_id=sibling.pk, operation=call.operation, request_key=call.request_key
    )
    with tenant_context(guide_world.tenant_id):
        own = serpro_retention._guide_anchors(call)
        foreign = serpro_retention._guide_anchors(stranger)
    assert [(a.basis, a.source_id) for a in own[0]] == [
        ("competencia_guia", guide_world.cycle.pk)
    ]
    assert foreign == ([], ["pedido_sem_ciclo"])


def test_a_cycle_link_does_not_erase_an_unresolved_federal_reference(
    guide_world: GuideWorld,
) -> None:
    _call, document = _obtain(guide_world)
    with tenant_context(guide_world.tenant_id):
        anchors, gap = retention_tasks._document_anchors(document)
        assert (gap, {a.basis for a in anchors}) == (
            "",
            {"ciclo", "competencia_guia"},
        )
        document.acquisition_receipt_ref = "sitfis-call-001"
        anchors, gap = retention_tasks._document_anchors(document)
    assert gap == "recibo_invalido"
    assert {a.basis for a in anchors} == {"ciclo"}


def test_a_guide_still_in_quarantine_is_kept(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    def closed(*args: object, **kwargs: object) -> None:
        message = "Este ciclo está encerrado."
        raise IllegalTransition(message)

    monkeypatch.setattr(pgmei, "_link_in_cycle", closed)
    _call, document = _obtain(guide_world)
    expiry = _expiry(guide_world.cycle.period_start.year)
    with tenant_context(guide_world.tenant_id):
        candidate = Candidate(document.client_id, "document", document.pk, expiry)
        assert disposal_refusal(candidate) == "verificacao_pendente"
    purge_tenant(guide_world.tenant_id, dry_run=False, now=expiry)
    assert not _claimed(guide_world.tenant_id, document.pk)


def _staff_original(world: World, payload: bytes) -> Document:
    """A private, clean staff upload dated by an old obligation (2018)."""
    termo = world.termo
    with tenant_context(termo.tenant.pk):
        kind, _ = ObligationType.objects.get_or_create(
            code="DAS", defaults={"name": "DAS", "periodicity": "monthly"}
        )
        obligation = Obligation.objects.create(
            tenant=termo.tenant,
            client=termo.client,
            obligation_type=kind,
            competence_month=date(2018, 9, 1),
            nominal_due_date=date(2018, 10, 20),
            resolved_due_date=date(2018, 10, 20),
        )
        document = Document.objects.create(
            tenant=termo.tenant,
            client=termo.client,
            obligation=obligation,
            storage_key=default_storage.save(
                "originals/staff-051.pdf", ContentFile(payload)
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


def test_a_receipt_reusing_a_staff_original_keeps_it(world: World) -> None:
    tenant_id = world.termo.tenant.pk
    staff = _staff_original(world, PDF)
    [payment] = _payments(world, _collected("2026-10-20"))
    call = _receipt(world, payment)
    with tenant_context(tenant_id):
        # Intake reused the staff upload; nothing records the receipt's use of it.
        assert not Document.objects.filter(
            acquisition_receipt_ref=str(call.pk)
        ).exists()
        anchors, gap = retention_tasks._document_anchors(staff)
    assert gap == "proveniencia_incompleta"
    assert [a.basis for a in anchors] == ["obrigacao"]
    purge_tenant(tenant_id, dry_run=False, now=_expiry(2030))
    assert not _claimed(tenant_id, staff.pk)
    assert default_storage.exists(staff.storage_key)


def test_a_call_of_an_unrelated_operation_never_dates_an_original(
    world: World,
) -> None:
    tenant_id = world.termo.tenant.pk
    _payments(world, _collected("2026-10-20"))
    with tenant_context(tenant_id):
        consultation = ProviderCall.objects.get(operation="SP-PAG")
    original = ingest_system_document(
        tenant_id=tenant_id,
        client_id=world.termo.client.pk,
        origin=AcquisitionOrigin.FEDERAL_PROVIDER,
        receipt_ref=str(consultation.pk),
        payload=b"%PDF-1.7\n% names a consultation call AXRM-051\n%%EOF\n",
        filename="consulta.pdf",
        content_type="application/pdf",
        retention_class="fiscal_evidence",
    )
    with tenant_context(tenant_id):
        assert retention_tasks._document_anchors(original)[1] == (
            "operacao_nao_reconhecida"
        )


def test_writers_refuse_bytes_whose_disposal_started(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    termo = world.termo
    staff = _staff_original(world, b"%PDF-1.7\n% staff original AXRM-051\n%%EOF\n")

    def refuse(key: str) -> None:
        message = "synthetic storage refusal"
        raise OSError(message)

    monkeypatch.setattr(default_storage, "delete", refuse)
    purge_tenant(termo.tenant.pk, dry_run=False, now=_expiry(2030))
    assert _claimed(termo.tenant.pk, staff.pk)
    with tenant_context(termo.tenant.pk):
        head = head_record(termo.client.pk, "sitfis")
    with tenant_context(termo.tenant.pk), pytest.raises(ValidationError):
        declare_authority(
            termo.actor,
            termo.client.pk,
            service="sitfis",
            code="00002",
            valid_from=timezone.localdate(),
            expected_version=head.version if head else 0,
            evidence_document=staff,
        )
    with pytest.raises(UploadRejected):
        ingest_system_document(
            tenant_id=termo.tenant.pk,
            client_id=termo.client.pk,
            origin=AcquisitionOrigin.FEDERAL_PROVIDER,
            receipt_ref=str(uuid4()),
            payload=b"%PDF-1.7\n% staff original AXRM-051\n%%EOF\n",
            filename="reuso.pdf",
            content_type="application/pdf",
            retention_class="fiscal_evidence",
        )


def test_the_signed_termo_writer_refuses_bytes_whose_disposal_started(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    termo = world.termo
    staff = _staff_original(world, b"%PDF-1.7\n% signed termo copy AXRM-051\n%%EOF\n")

    def locked() -> None:
        with tenant_context(termo.tenant.pk), transaction.atomic():
            connection = ProviderConnection.objects.get(pk=termo.connection.pk)
            submission = TermoSubmission(
                connection.pk, staff.pk, connection.version, termo.parties
            )
            authorization._locked_document(termo.actor, submission)

    locked()  # a clean, unclaimed staff original is accepted

    def refuse(key: str) -> None:
        message = "synthetic storage refusal"
        raise OSError(message)

    monkeypatch.setattr(default_storage, "delete", refuse)
    purge_tenant(termo.tenant.pk, dry_run=False, now=_expiry(2030))
    assert _claimed(termo.tenant.pk, staff.pk)
    with pytest.raises(TermoInvalidoError):
        locked()


@pytest.mark.parametrize(
    ("state", "reuse_possible"),
    [
        (CallState.STARTED, True),
        (CallState.UNCERTAIN, True),
        (CallState.FAILED, False),
    ],
)
def test_a_call_that_may_have_stored_bytes_keeps_private_originals(
    world: World, state: str, reuse_possible: bool
) -> None:
    tenant_id = world.termo.tenant.pk
    staff = _staff_original(world, b"%PDF-1.7\n% staff original AXRM-051 C20\n%%EOF\n")
    [payment] = _payments(world, _collected("2026-10-20"))
    issued = _receipt(world, payment)
    with tenant_context(tenant_id):
        # A second receipt call left in `state`, with no original of its own: a
        # lost runner may already have stored (and deduplicated) its bytes.
        columns = {
            field.attname: getattr(issued, field.attname)
            for field in ProviderCall._meta.concrete_fields
            if not field.primary_key
        }
        columns.update(state=state, request_key=f"{issued.request_key[:40]}-{state}")
        ProviderCall.objects.create(**columns)
        gap = retention_tasks._document_anchors(staff)[1]
    assert gap == ("proveniencia_incompleta" if reuse_possible else "")
    purge_tenant(tenant_id, dry_run=False, now=_expiry(2030))
    assert _claimed(tenant_id, staff.pk) is not reuse_possible


def test_a_call_that_never_stored_bytes_never_dates_an_original(world: World) -> None:
    tenant_id = world.termo.tenant.pk
    [payment] = _payments(world, _collected("2026-10-20"))
    issued = _receipt(world, payment)
    original = _original(tenant_id, issued)
    with tenant_context(tenant_id):
        columns = {
            field.attname: getattr(issued, field.attname)
            for field in ProviderCall._meta.concrete_fields
            if not field.primary_key
        }
        columns.update(
            state=CallState.QUEUED, request_key=f"{issued.request_key[:40]}-q"
        )
        queued = ProviderCall.objects.create(**columns)
        original.acquisition_receipt_ref = str(queued.pk)
        assert serpro_retention._acquisition_anchors(original) == (
            [],
            ["chamada_sem_bytes"],
        )


def test_private_reuse_reads_claims_under_the_client_lock(world: World) -> None:
    termo = world.termo
    payload = b"%PDF-1.7\n% reused under the lock AXRM-051\n%%EOF\n"
    staff = _staff_original(world, payload)
    with CaptureQueriesContext(connection) as queries:
        reused = ingest_system_document(
            tenant_id=termo.tenant.pk,
            client_id=termo.client.pk,
            origin=AcquisitionOrigin.FEDERAL_PROVIDER,
            receipt_ref=str(uuid4()),
            payload=payload,
            filename="reuso.pdf",
            content_type="application/pdf",
            retention_class="fiscal_evidence",
        )
    assert reused.pk == staff.pk
    statements = [query["sql"] for query in queries.captured_queries]
    locked = [
        index
        for index, sql in enumerate(statements)
        if "clients_clientcompany" in sql and "FOR NO KEY UPDATE" in sql
    ]
    claims = [
        index
        for index, sql in enumerate(statements)
        if "cycles_contentpurgeclaim" in sql
    ]
    assert locked
    assert claims
    assert locked[0] < claims[0]


def test_an_archive_of_a_staff_first_guide_is_unsupported_on_versioned_storage(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    termo = guide_world.termo
    with tenant_context(guide_world.tenant_id):
        client = ClientCompany.objects.get(pk=guide_world.cycle.client_id)
        Document.objects.create(
            tenant=termo.tenant,
            client=client,
            storage_key=default_storage.save(
                f"originals/{uuid4().hex}.pdf", ContentFile(GUIDE_PDF)
            ),
            sha256=hashlib.sha256(GUIDE_PDF).hexdigest(),
            original_filename="guia-da-equipe.pdf",
            content_type="application/pdf",
            byte_size=len(GUIDE_PDF),
            uploaded_by=guide_world.owner,
            acquisition_origin=AcquisitionOrigin.UPLOAD_STAFF,
            portal_visible=False,
        )
    with FakeSerpro((das_reply(),)) as peer:
        pgmei.obtain_das_guide(
            guide_world.owner,
            guide_world.tenant_id,
            guide_world.cycle.pk,
            origin=peer.origin,
        )
    with tenant_context(guide_world.tenant_id):
        # The guide reused the staff upload: no SERPRO original, no report.
        assert not Document.objects.filter(
            acquisition_origin=AcquisitionOrigin.FEDERAL_PROVIDER
        ).exists()
        bucket = S3Storage(
            bucket_name="axrm051-synthetic",
            endpoint_url="https://storage.invalid",
            access_key="synthetic-access",
            secret_key="synthetic-secret",  # noqa: S106 - no request is ever sent
        )
        monkeypatch.setattr(retention, "default_storage", bucket)
        archive = Candidate(
            client.pk, "export", uuid4(), None, (f"exports/{uuid4().hex}.zip",)
        )
        assert disposal_refusal(archive) == "armazenamento_nao_suportado"
